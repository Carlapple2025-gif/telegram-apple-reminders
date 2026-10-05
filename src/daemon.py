#!/usr/bin/env python3
"""
收件守护：常驻监听 Telegram，收到即写入对应 App。

## 唯一的职责

    收到一条消息 → `intake.handle()` → 写进 App → 回执

**它不做任何判断。** 类型由你写的行首符号声明（见 `routes.py` /
[`docs/SYMBOL-SCHEME.md`](../docs/SYMBOL-SCHEME.md)），
守护只负责"收到就交出去"。

## 唯一的状态：offset 必须持久化

Telegram 的 `getUpdates` 靠 offset 推进读取位置。offset 只存在内存里的话：

  · 进程重启 → 重新拉到旧消息 → **同一条被记两遍**（重复建待办）
  · 崩溃恢复 → 同上

所以 offset 落盘到 `data/daemon-state.json`。
**跨调用的读取位置必须持久，不能只活在内存里。**

## 为什么这一层没有别的状态了

曾经有第二类状态：`data/pending/`（"判不准 → 发按钮 → 等你点"）。
2026-10-03 整体删除（裁决见 SYMBOL-SCHEME §4.6），因为它要跨消息
记住"在等什么"，命中 `ARCHITECTURE.md` §十一 判据 1
（✗ 需要跨两次交互记住东西），并贡献了两次真实故障。

删掉之后，Telegram 层只剩 `offset` 这一个状态 —— 它是**通道机制**，
与业务无关。业务上没有任何状态，就不可能和 Apple 应用不一致。
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import telegram as tg          # noqa: E402
import journal                 # noqa: E402
import commands                # noqa: E402
from intake import Intake      # noqa: E402
from kinds import Kind         # noqa: E402

STATE_FILE = ROOT / "data" / "daemon-state.json"
# 注：这里曾有 `PENDING_DIR = ROOT / "data" / "pending"`，
# 随按钮/待补充机制一起删除（见下面那段说明）。留着它会让人以为
# 还有第二类状态 —— 而"以为还有"本身就是排查时的成本。

# 日志里引用用户内容时的截断长度。
# 程序日志（logs/）可能被贴出来排查问题，不该带太多个人信息。
TRUNC = 30


# ── 启动通知：聊天里唯一的"用法说明"
#
# ⚠️ 这几条样例必须与 `routes.py` 的符号表**逐条对得上**。
# 这里踩过一次真实的坑：符号方案落地后，这条通知还在教"不打符号也能进日历/备忘录"，
# 而它是你唯一能在聊天里看到的说明书（没有 /help，也没有 setMyCommands）——
# 于是**一天被教错十次**（实测 17.6 小时内进程启动 13 次）。
# 详见 docs/TELEGRAM-VOICE.md 的 V1。
#
# 所以样例写成**数据**而不是一段手写的字面量：自检里有一条断言把下面每一条
# 真的丢进 `routes.route()` 验一遍。以后谁改了符号语义，这里会先红 ——
# 而不是等你收几天错的说明书才发现。
USAGE_EXAMPLES: list[tuple[str, str]] = [
    ("交电费", "提醒事项"),                     # 裸输入 = 待办（最高频）
    ("# 学原理比学语法重要", "备忘录"),           # # = 备忘
    ("@周五下午两点 项目周会", "日历"),           # @ = 日程
]

STARTUP_NOTICE = (
    "👋 收件守护已启动。直接发一句就行"
    "（行首符号决定去哪，不写符号就是待办）：\n"
    + "\n".join(f"　　「{t}」→ {where}" for t, where in USAGE_EXAMPLES)
    + "\n\n查现在有什么：发 /list（待办 + 今天/明天的日程）"
)


# ── 非文本消息：如实回一句，而不是静默丢掉
#
# 语音 / 图片 / 文件此前是**完全静默**的：没有回执、日志里也没有一行，
# 表现等于"它死了"。这是一句**陈述**（我做不到），不是提问 ——
# 不引入任何跨消息状态，所以不违反 ARCHITECTURE §十一 的判据。
# 详见 docs/TELEGRAM-VOICE.md 的 V4。
NON_TEXT_REPLY = (
    "📎 这条我没法记：我只认文字消息\n"
    "　　（语音 / 图片 / 文件都读不了 —— 请把内容打字发我）"
)

# Telegram 消息里表示"内容不是文字"的字段 → 给人看的说法。
# 用来在日志和回执里说清"收到的是什么"，而不是笼统的"非文本"。
_NON_TEXT_FIELDS: list[tuple[str, str]] = [
    ("photo", "图片"), ("voice", "语音"), ("audio", "音频"),
    ("document", "文件"), ("video", "视频"), ("video_note", "视频留言"),
    ("sticker", "贴纸"), ("animation", "动图"), ("location", "位置"),
    ("contact", "联系人"), ("poll", "投票"), ("dice", "骰子"),
]


def non_text_kind(msg: dict) -> str:
    """
    这条消息带的是什么非文字内容？没有则返回空串。

    服务类消息（有人入群、消息被置顶…）不带这些字段，于是仍然安静跳过 ——
    "不是文字"和"不是内容"要分开：前者该回一句，后者不该。
    """
    for field, label in _NON_TEXT_FIELDS:
        if msg.get(field):
            return label
    return ""


# ── 回执发送：带重试
#
# 为什么重试放在**这里**而不是 `telegram.send()`：
# 通道层那条注释写明了「不做自动重试 —— 静默重试会让'没发出去'变成
# '以为发出去了'」。那个理由依然成立，所以重试放在**看得见失败的调用方**：
# 这里能如实记进 journal，也不会把失败咽掉。
#
# 为什么需要它：网络抖动是常态（实测 17.6 小时里 176 次拉取失败），
# 而**回执发不出去时条目已经写进 App 了** —— 你那边看到的是"毫无反应"，
# 自然反应是重发一遍 → 重复条目。详见 docs/TELEGRAM-VOICE.md 的 V3。
REPLY_RETRY_DELAYS = (5, 15)     # 秒：第一次失败等 5 秒，再失败等 15 秒


def send_receipt(text: str, silent: bool) -> bool:
    """
    发一条回执。失败按 `REPLY_RETRY_DELAYS` 重试。返回最终是否发出。

    `silent=True` → `disable_notification`：不响不震（横幅与未读角标仍在）。
    分档规则见 docs/TELEGRAM-VOICE.md 的 V2：**成功静音、失败有声**。

    **绝不抛异常**：它跑在收件主路径上，发不出去不该把守护带崩
    （崩了就漏掉后面所有消息）。最终失败会记进 journal 的 error 读数。
    """
    last = ""
    for attempt in range(len(REPLY_RETRY_DELAYS) + 1):
        if attempt:
            time.sleep(REPLY_RETRY_DELAYS[attempt - 1])
        try:
            tg.send(text, disable_notification=silent)
            if attempt:
                _log(f"回执重试第 {attempt} 次成功")
            return True
        except tg.TelegramError as e:
            # 文案带换行提示，只留第一行（与 run_once 的取舍一致）
            last = str(e).splitlines()[0]
            _log(f"回执发送失败（第 {attempt + 1} 次）：{_trunc(last)}")
        except Exception as e:  # noqa: BLE001
            # 非通道类异常 = 编程错误，重试没有意义：如实记下就放弃。
            # （刻意不把它归成"网络又坏了"，否则真 bug 会被说成抖动。）
            last = f"{type(e).__name__}: {e}"
            _log(f"回执发送异常（不重试）：{_trunc(last)}")
            break

    try:
        journal.log_error(where="receipt", detail=f"回执最终未发出：{last}")
    except Exception:  # noqa: BLE001
        pass
    return False

_running = True


def _log(msg: str) -> None:
    """写一行程序日志（stdout 会被 launchd 收进 logs/）。"""
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _trunc(s: str) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= TRUNC else s[:TRUNC] + "…"


# ── 状态持久化

def load_offset() -> int | None:
    if not STATE_FILE.is_file():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("offset")
    except (json.JSONDecodeError, OSError):
        return None


def update_state(**fields) -> None:
    """
    **读-改-写**地更新状态文件（保留其它字段）。

    ⚠️ 不能用"写一个全新字典"的方式 —— 那会把别的字段冲掉。
    实测踩到：save_offset 原本写的是全新字典，于是每次保存读取位置
    都会把 notified_at（启动通知冷却用）抹掉，冷却随即失效、
    崩溃循环时又会开始刷屏。
    这类"读-改-写没保留其它字段"的坑在本项目出现过多次（早先的
    Telegram 配置也是），所以统一收口到一个函数。
    """
    data: dict = {}
    if STATE_FILE.is_file():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data.update(fields)
    data["at"] = dt.datetime.now().astimezone().replace(
        microsecond=0).isoformat()
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)     # 原子替换，避免写一半被读到
    except OSError as e:
        _log(f"状态保存失败（不影响运行）：{_trunc(str(e))}")


def save_offset(offset: int) -> None:
    """保存读取位置。**保留其它字段**（见 update_state 的说明）。"""
    update_state(offset=offset)


# ── （已删除）待补充项与按钮
#
# 这里曾有约 120 行：`save_pending` / `load_pending` / `clear_pending` /
# `PENDING_TTL_HOURS` / `_ask_keyboard` / `_handle_callback` / `_dispatch_forced` ——
# 一整套"判不准 → 发按钮 → 等你点 → 补时间"的跨消息状态机。
#
# **2026-10-03 整体删除**（裁决见 docs/SYMBOL-SCHEME.md §4.6）：
#   · 类型现在由**你写的符号**声明，不存在"判不准"，也就没有要问的
#   · 这套机制要跨消息记住"在等什么"，命中 ARCHITECTURE.md §十一 判据 1
#   · 它贡献了两次真实故障：待补充槽位被清、按钮绑错了消息号
#
# 删除后 Telegram 层只剩一个状态：`offset`（通道必需，与业务无关）。
# 缺什么信息就**带符号重发一条完整的** —— 代价是重打几个字，
# 收益是这一类故障从根上不存在。


# ── 主循环

# 离线模式：把三个写入端换成假的，用于在**没有授权**时先看整条链路
# 怎么工作（回执文案、分类结果、日志记录）。真实运行不用这个。
_OFFLINE = False


def _make_intake() -> Intake:
    if not _OFFLINE:
        return Intake()

    calls: list[str] = []

    def _t(text, when=None):
        calls.append(f"提醒事项 ← {text}")
        return "OFFLINE-TODO"

    def _e(summary, start, end, location="", recurrence="", allday=False):
        calls.append(f"日历 ← {summary} @ {start} [{recurrence or '不重复'}]")
        return "OFFLINE-EVENT"

    def _m(text):
        calls.append(f"备忘录 ← {text}")
        return "OFFLINE-MEMO"

    # 离线模式是**测试性质**：它必须写到临时目录，不能污染真实台账
    # （实测踩过：7 条测试记录写进了真实 journal）。
    # 用 testing=True 声明，而不是让写入点去猜。
    it = Intake(add_todo=_t, add_event=_e, add_memo=_m, testing=True)
    it._offline_calls = calls        # 供回执里展示"会写到哪里"
    return it


def handle_message(text: str, msg_id: int, chat: str,
                   base: dt.date | None = None) -> None:
    """
    处理一条收到的消息。

    `base` **只给测试用**：生产永远传 None（= 真实的今天）。
    自检里若不给它，"明天"就随运行日期漂移 ——
    断言写死 10-04 而实际算成 10-05，跨过午夜就会失败。
    那种断言测的是"今天是几号"，不是代码对不对。
    """
    _log(f"收到：{_trunc(text)!r}")
    # 让等待看得见（V9）：写 AppleScript 要几秒（冷启动更久），
    # 而这条消息在聊天里此前是"发完没动静"。不产生消息、失败也不影响任何事。
    tg.send_chat_action("typing")
    it = _make_intake()

    # 类型由符号声明（routes.py），所以这里**没有**任何"上一条在等什么"
    # 的上下文要传 —— 那类跨消息状态已随追问机制一起删除。
    out = it.handle(text, base=base, msg_id=msg_id)

    reply = out.reply
    if _OFFLINE:
        calls = getattr(it, "_offline_calls", [])
        if calls:
            reply += "\n\n【离线模式】实际会写：" + "；".join(calls)
        else:
            reply += "\n\n【离线模式】不写入任何东西"
    # 音量分档：**成功静音、失败有声**（docs/TELEGRAM-VOICE.md V2）。
    # 成功时你人就在聊天界面里，横幅不提供任何新信息；
    # 而"没记成"可能是唯一能提醒你的信号，必须能把你叫醒。
    send_receipt(reply, silent=out.ok)

    # `needs_ask` 分支已删除：不再有"判不准就问一次"。
    # 写成功与否都只记一行，用户从回执本身就能看出结果。
    _log(f"→ {out.kind.value if out.kind else '?'} ok={out.ok}")


def run_once(offset: int | None = None, wait: int = 25) -> int | None:
    """
    拉一次更新并处理。返回新的 offset。

    `wait` 是长轮询的等待秒数：
      · 常驻运行时用 25（省请求、响应快）
      · `--once` 验证时用 1（否则要干等 25 秒才返回，很别扭）

    首次调用会先消费历史（由 main 负责），避免把旧消息又记一遍。
    """
    try:
        ups = tg.get_updates(offset=offset, limit=20, timeout=wait)
    except tg.TelegramError as e:
        # 只记第一行 —— TelegramError 的文案带换行提示，长时间断网时
        # 每次重试都打一遍会刷满日志（实测：断网 12 秒就打了 2 遍完整提示）。
        _log(f"拉取失败：{str(e).splitlines()[0][:70]}")
        time.sleep(5)
        return offset

    if not ups:
        return offset

    new_offset = offset
    for u in ups:
        new_offset = max(new_offset or 0, u.get("update_id", 0) + 1)

        # 回调（按钮点击）不再处理：按钮机制已整体删除。
        # 但**必须回应一句** —— 否则客户端上那个按钮会一直转圈：
        # 老消息还留在聊天记录里，点一下就是永远转圈（V7）。
        #
        # 为什么不用 drain_stale_callbacks（它能回应 + 撤按钮）：它在末尾会把
        # **Telegram 侧的读取位置**推到最新，而那不是我们保存的 offset ——
        # 守护停机期间收到的真消息会被一起跳掉。这就是"顺手复用"的陷阱。
        if "callback_query" in u:
            cb = u.get("callback_query") or {}
            tg.answer_callback(cb.get("id", ""),
                               text="这条已过期（按钮机制已删除）")
            _log("忽略按钮回调（按钮机制已删除），已回应「这条已过期」")
            continue

        msg = u.get("message")
        if not msg:
            continue
        # 只处理目标会话的消息（bot 可能被拉进别的会话）
        _, cfg_chat = tg.load_config()
        chat = str(msg.get("chat", {}).get("id", ""))
        if cfg_chat and chat != str(cfg_chat):
            _log(f"忽略非目标会话的消息（chat {chat}）")
            continue

        text = (msg.get("text") or "").strip()
        if not text:
            # 非文本消息此前是**静默丢弃**（日志都没有一行），表现等于"它死了"。
            # 现在如实回一句。服务类消息不带内容字段 → non_text_kind 返回空 → 仍然安静跳过。
            kind = non_text_kind(msg)
            if kind:
                _log(f"收到非文本消息（{kind}），未写入任何 App")
                send_receipt(NON_TEXT_REPLY, silent=False)
            continue

        # ── 命令（只读查询）。判据是**字面**的"以 / 开头"（见 commands.py 顶部）
        #
        # 必须在收件**之前**分流：命令不写任何 App —— 若放它过去，
        # `/list` 会变成一条标题是 "/list" 的待办（裸输入默认待办）。
        #
        # 这不算"守护开始做判断"：判断的只是"这串字是不是命令"，
        # 与上面"这条消息是不是文字"同级，不涉及任何业务语义。
        cmd = commands.match(text)
        if cmd is not None:
            reply, ok = commands.run(cmd)
            # 留痕只进 logs/（程序日志），不进 journal（你的内容留痕）——
            # 命令是读操作，不产生任何要复盘的内容。见 commands.py 末尾说明。
            _log(f"命令 {text!r} → ok={ok}")
            # ⚠️ 把回执里的"读不到"也转进日志：否则日志只说 ok=True，
            # 而"某个 App 没读到"这件事只存在于你看到的那条消息里 ——
            # 排查时就回答不了"刚才那次日历到底读到没有"（真实踩到：
            # 用户问 /list 的结果对不对，日志里看不出来）。
            for _line in reply.splitlines():
                if "读不到" in _line:
                    _log(f"  ⚠️ {_line.strip()[:70]}")
            send_receipt(reply, silent=ok)
            continue

        try:
            handle_message(text, msg.get("message_id", 0), chat)
        except Exception as e:  # noqa: BLE001
            # 单条失败不能让守护进程崩 —— 崩了就漏掉后面所有消息
            _log(f"处理失败：{type(e).__name__}: {_trunc(str(e))}")
            # 这条同样走 send_receipt：它自带重试且不抛异常
            # （原先这里再套一层 try/except 吞掉失败，等于"连报错都发不出去"）。
            send_receipt(
                f"❌ 这条没处理成：{type(e).__name__}\n"
                f"　　（你可以直接在对应 App 里手动加）",
                silent=False)

    save_offset(new_offset)
    return new_offset


# 启动通知的冷却时间（秒）。
#
# 原先 300 秒。实测 17.6 小时内进程启动 13 次，这条通知因此重复出现约 10 次
# （见 docs/TELEGRAM-VOICE.md 附录 D）—— 它的内容是**用法说明**，
# 说一遍就够，重复说只是刷聊天记录。
NOTIFY_COOLDOWN = 3600


def _should_notify_startup() -> bool:
    """距上次启动通知是否已超过冷却时间。"""
    if not STATE_FILE.is_file():
        return True
    try:
        last = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("notified_at")
    except (json.JSONDecodeError, OSError):
        return True
    if not last:
        return True
    try:
        prev = dt.datetime.fromisoformat(last)
    except ValueError:
        return True
    age = (dt.datetime.now().astimezone() - prev).total_seconds()
    return age >= NOTIFY_COOLDOWN


def _mark_notified() -> None:
    """记下"刚发过启动通知"（经 update_state，保留 offset 等字段）。"""
    update_state(notified_at=dt.datetime.now().astimezone().replace(
        microsecond=0).isoformat())


# 退出原因。区分"主动停止"与"异常退出"很关键 ——
# launchd 的 KeepAlive 只重启**非正常退出**的进程。
# 所以收到信号（launchd 要求停止）应算正常退出，而异常应算失败。
_stopped_by_signal = False


def _stop(signum, frame):  # noqa: ANN001
    global _running, _stopped_by_signal
    _running = False
    _stopped_by_signal = True
    _log(f"收到信号 {signum}，准备退出（正常停止，不应被重启）")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="收件守护：常驻监听 Telegram")
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮就退出（测试用）")
    ap.add_argument("--drain", action="store_true",
                    help="只把历史更新消费掉（不处理内容），用于首次启动前定基准")
    ap.add_argument("--reset", action="store_true",
                    help="清掉保存的 offset（下次从最新开始）")
    ap.add_argument("--offline", action="store_true",
                    help="离线模式：不写入任何 Apple 应用，只回执「会写到哪里」")
    args = ap.parse_args()

    global _OFFLINE
    _OFFLINE = args.offline
    if _OFFLINE:
        _log("离线模式：不会写入任何 Apple 应用")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.reset:
        try:
            STATE_FILE.unlink(missing_ok=True)
            _log("已清除 offset")
        except OSError as e:
            _log(f"清除失败：{e}")
        return 0

    if args.drain:
        # 把当前积压全部消费掉，并把 offset 定为"最新之后"。
        # 首次启动前跑一次，可以避免把几天前的旧消息当成新输入。
        try:
            ups = tg.get_updates(limit=100)
        except tg.TelegramError as e:
            _log(f"拉取失败：{e}")
            return 2
        if ups:
            off = max(u.get("update_id", 0) for u in ups) + 1
            save_offset(off)
            _log(f"已消费 {len(ups)} 条历史更新，offset 定为 {off}")
        else:
            _log("没有积压更新")
        # 推进 Telegram 侧的位置
        cur = load_offset()
        if cur:
            try:
                tg.get_updates(offset=cur, limit=1)
            except tg.TelegramError:
                pass
        return 0

    offset = load_offset()
    if offset is None:
        _log("首次启动：先消费历史更新，避免把旧消息当成新输入")
        # ⚠️ 这里**不能退出**。launchd 的 KeepAlive 会每 ThrottleInterval 秒
        # 重启一次，于是"启动时 Telegram 不可用"会变成**无限崩溃循环** ——
        # 日志刷满、而且永远恢复不了（每次都死在同一步）。
        # 正确做法：记一笔、把 offset 留空，让主循环去重试。
        try:
            ups = tg.get_updates(limit=100)
            offset = (max(u.get("update_id", 0) for u in ups) + 1) if ups else None
            if offset:
                save_offset(offset)
        except tg.TelegramError as e:
            _log(f"首次定基准失败（{_trunc(str(e))}）—— 交给主循环重试")
            offset = None
    _log(f"启动，offset = {offset}")

    # 启动通知带冷却：只在距上次通知超过 NOTIFY_COOLDOWN 秒时才发。
    #
    # 为什么需要：守护是 KeepAlive 的，如果因故反复重启，
    # **你每次都会收到一条启动消息** —— 崩溃循环会变成消息轰炸，
    # 而那恰恰是你最不想被打扰的时候。
    if _should_notify_startup():
        try:
            # 静音：这条是"一切正常"的播报，不该在你锁屏时响一声。
            # 音量分档见 docs/TELEGRAM-VOICE.md 的 V2（成功静音、失败有声）。
            tg.send(STARTUP_NOTICE, disable_notification=True)
            _mark_notified()
        except tg.TelegramError as e:
            _log(f"启动通知发送失败（不影响运行）：{_trunc(str(e))}")
    else:
        _log("（启动通知处于冷却期，跳过 —— 避免反复重启时刷屏）")

    # 把 `/` 菜单推上去（私聊里打 `/` 时的自动补全列表）。
    #
    # 这是 Telegram **服务端持久**的设置，本来设一次就够；每次启动推一遍是
    # 为了"改了命令表 → 重启即生效"，不用记得手动同步（少一个会忘记的步骤）。
    # 失败只记一行：菜单设不上不影响收件。
    if tg.set_my_commands(commands.MENU):
        _log("已设置「/」菜单：" + "、".join(f"/{c}" for c, _ in commands.MENU))
    else:
        _log("「/」菜单设置失败（不影响收件）：网络不可用或未配置 token")

    # 看门狗在**循环之外**只导入一次，且失败就降级为大白话一行日志。
    #
    # 为什么不在循环里 `import watchdog`：那条 import 一旦失败
    # （文件被改坏、语法错、被误删），ImportError/SyntaxError 不会被循环的
    # `except (OSError, HTTPException)` 兜住 → 进程退出 → KeepAlive 拉起
    # → 再退出 = **崩溃循环**，而那期间你发的消息没人接。
    # 收件是守护的唯一职责，一个附加组件不该有能力把它带停。
    # （也不能静默：失败必须留下一行看得见的原因。）
    #
    # 放在 `_consecutive_failures` 之上是有意的：下面那段主循环的静态检查
    # 断言"兜底只兜瞬时类、不吞编程错误"，这里的宽 except 属于**循环之外**
    # 的降级路径，不该混进那条断言的作用范围。
    _watchdog = None
    if not _OFFLINE:
        try:
            import watchdog as _watchdog
        except Exception as e:  # noqa: BLE001 —— 故意的：降级而不是崩
            _log(f"看门狗不可用（不影响收件）：{type(e).__name__}: "
                 f"{_trunc(str(e))}")

    _consecutive_failures = 0
    while _running:
        # 主循环这道兜底是**最后一道**（run_once 内部已各自兜住网络错与单条消息错）：
        # offset 落盘、snapshot 读取等路径若抛出 OSError，异常会冒到 main 之外，
        # 进程退出 → KeepAlive 拉起 → 几步后又退出 = 崩溃循环，
        # 而那期间**你发的消息没人接**。
        #
        # 只兜"瞬时类"（网络/socket/落盘）。**编程错误故意不兜**：
        # 让它退出并留下完整堆栈，比带着坏状态空转更容易查。
        try:
            # --once 用短轮询：验证时不该干等长轮询的超时
            offset = run_once(offset, wait=1 if args.once else 25)
            _consecutive_failures = 0
            # 日报看门狗：到点检查"今天那份送出没有"（不新增状态、每天最多一次、
            # 内部吞掉一切异常 —— 见 watchdog.py 顶部说明）。
            # 放在这里而不是单独一个定时任务：守护本来就在跑，多一个任务就多一处
            # "装了没生效"的可能（本项目在 launchd 上踩过两次）。
            if _watchdog is not None:
                _watchdog.tick()
        except (OSError, http.client.HTTPException) as e:
            _consecutive_failures += 1
            _log(f"循环异常（第 {_consecutive_failures} 次，继续运行）："
                 f"{type(e).__name__}: {_trunc(str(e))}")
            # 连续失败时逐步退避，避免刷日志刷到把真问题淹掉
            time.sleep(min(5 * _consecutive_failures, 60))
        if args.once:
            break
        time.sleep(1)

    _log("已退出")
    # 收到信号 → 正常停止（返回 0，KeepAlive 不重启）；
    # 异常跳出循环 → 返回非 0，让 KeepAlive 把守护拉起来。
    return 0 if _stopped_by_signal else 1


if __name__ == "__main__":
    sys.exit(main())
