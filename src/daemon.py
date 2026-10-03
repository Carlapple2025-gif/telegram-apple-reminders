#!/usr/bin/env python3
"""
收件守护：常驻监听 Telegram，收到即分派。

## 两条关键设计

### ① offset 必须持久化

Telegram 的 `getUpdates` 靠 offset 推进读取位置。offset 只存在内存里的话：

  · 进程重启 → 重新拉到旧消息 → **同一条被记两遍**（重复建待办）
  · 崩溃恢复 → 同上

所以 offset 落盘到 `data/daemon-state.json`。这条与 v1 的按钮 bug 同源：
**跨调用的读取位置必须持久，不能只活在内存里。**

### ② 确认按钮是"动作触发点"，不是"对话载体"

分类不确定时给三个按钮（待办/日程/备忘），点一下就把这条补记到对应应用。
严格守住（docs/ARCHITECTURE.md 的防掉坑判据）：

  · 不需要跨两次点击记住东西 —— callback_data 里自带 message_id 和选择
  · 不需要重绘整条消息体现进度 —— 点完就回复一句，不重绘
  · 没有超时/重入/确认屏 —— 按钮点晚了也能用（只要那条消息还在）

原始文本从 `data/pending/` 取（按 message_id 命名），所以按钮不依赖内存状态。

## 为什么不做成"对话"

v1 的时段选择器之所以失控（551 行），是因为它把"问一次"做成了
"一段有状态的对话"。这里刻意只做**一问一答**，答完即止。
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
from intake import Intake      # noqa: E402
from classify import Kind      # noqa: E402

STATE_FILE = ROOT / "data" / "daemon-state.json"
PENDING_DIR = ROOT / "data" / "pending"

# 日志里引用用户内容时的截断长度。
# 程序日志（logs/）可能被贴出来排查问题，不该带太多个人信息。
TRUNC = 30

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


# ── 待补充项（按钮 + "补时间"共用）
#
# 原始文本落盘，所以按钮不依赖内存 —— 进程重启后按钮照样能用。
#
# ⚠️ **只有一个槽位。** 这是刻意的，也是被实测逼出来的：
#
# 最初按消息号存（`<msg_id>.json`），于是"用户点了按钮 / 系统发出追问 /
# 用户回一句时间"这一串下来会留下**两份**记录（原消息一份、追问一份），
# 而补时间时系统面对两份就只能"猜接哪一条" —— 端到端实测的结果是
# `有多条待补充，无法确定接哪条 → 不合并`，修复等于没生效。
#
# 改成一个槽位后：新的待补充来了就**替掉**旧的，
# 于是"最近一条还没处理完的"天然只有一份，不需要猜。
# 按钮仍然能用 —— 消息号存在记录**里面**（`msg_id` 字段），
# 回调时按它校验"这个按钮是不是还对应着当前这条"。

PENDING_TTL_HOURS = 24


def _pending_path() -> Path:
    return PENDING_DIR / "current.json"


def save_pending(msg_id: int, text: str) -> None:
    """把"当前待补充的那条"存下来（**替换**掉上一份）。"""
    try:
        # 写之前先确保目录在 —— 实测被自检打脸过：目录被清掉后
        # 这里直接 FileNotFoundError 抛出，而它跑在收件主路径上。
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _pending_path().write_text(
            json.dumps({"text": text, "msg_id": msg_id,
                        "at": dt.datetime.now().isoformat()},
                       ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        # 存不下不该让收件失败（与 journal 同一条原则：状态坏了不影响主流程）
        _log(f"待补充状态保存失败（不影响回执）：{_trunc(str(e))}")


def _pending_file() -> Path | None:
    """
    当前待补充项**实际所在的文件**（没有则 None）。

    正常情况下就是 `current.json`；若只存在旧格式（`<msg_id>.json`）则返回它 ——
    读、清都必须用同一个解析结果，否则会出现"读到了旧的、清的却是新文件"
    这种既丢状态又留垃圾的组合。
    """
    cur = _pending_path()
    if cur.is_file():
        return cur
    if not PENDING_DIR.is_dir():
        return None
    legacy = [q for q in PENDING_DIR.glob("*.json")]
    if not legacy:
        return None
    return max(legacy, key=lambda q: q.stat().st_mtime)


def _load_pending_record() -> dict | None:
    p = _pending_file()
    if p is None:
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(rec, dict) or not rec.get("text"):
        return None
    rec.setdefault("msg_id", None)
    # 过期就不认了 —— 隔了一天的"补时间"接上去只会更让人困惑
    at = rec.get("at") or ""
    try:
        when = dt.datetime.fromisoformat(at)
        if dt.datetime.now() - when > dt.timedelta(hours=PENDING_TTL_HOURS):
            return None
    except ValueError:
        pass
    return rec


def load_pending(msg_id: int | None = None) -> str | None:
    """
    取"当前待补充"的原文。

    `msg_id` 是**兼容参数**：只有一个槽位，"当前槽位"就是唯一答案，
    所以这里不再拿消息号去卡。踩过的坑：追问是**另一条新消息**，
    按钮却绑在原消息号上，两边一对不上，用户点按钮只得到
    "这条已经处理过了（或已过期）"—— 而追问文案还在让他点按钮。
    与其维护"哪个 id 才算数"，不如让按钮就解析当前槽位（TTL 负责挡过期）。
    """
    rec = _load_pending_record()
    if rec is None:
        return None
    return rec.get("text")


def clear_pending(msg_id: int | None = None) -> None:
    """
    清掉待补充项（清的就是"当前那个"，只有一个）。

    ⚠️ 这是本模块唯一的删除动作，删的是**自己刚写的临时文件**，
    不碰任何 Apple 应用里的东西 —— 不违反"agent 不删"的约束。

    `msg_id` 保留为兼容参数，仅记一行日志（见 load_pending 的说明）。
    """
    if msg_id is not None:
        rec = _load_pending_record()
        if rec is not None and rec.get("msg_id") not in (None, msg_id):
            _log(f"清槽位的调用来自 msg {msg_id}，槽位属于 "
                 f"msg {rec.get('msg_id')} —— 仍是同一个槽位，照清")
    p = _pending_file()
    if p is None:
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


# ── 按钮

_CB_CARRY = {"t": Kind.TODO, "e": Kind.EVENT, "m": Kind.MEMO}


def _ask_keyboard(msg_id: int) -> list[list[tuple[str, str]]]:
    # callback_data 上限 64 字节 —— 只放"选择 + 消息号"，不放正文
    return [[("待办", f"t:{msg_id}"), ("日程", f"e:{msg_id}"), ("备忘", f"m:{msg_id}")]]


def _handle_callback(data: str, callback_id: str, chat: str) -> None:
    """
    处理一次按钮点击。**一问一答，答完即止。**

    这里严守"动作触发点"定位：不重绘原消息、不需要跨点击状态。
    """
    tg.answer_callback(callback_id, text="收到")

    kind_code, _, mid_s = data.partition(":")
    kind = _CB_CARRY.get(kind_code)
    if kind is None or not mid_s.isdigit():
        return
    msg_id = int(mid_s)

    text = load_pending(msg_id)
    if text is None:
        tg.send("这条已经处理过了（或已过期）。")
        return

    _log(f"按钮：把 {_trunc(text)!r} 记为 {kind.value}")
    it = _make_intake()
    # 用强制类型走同一条分派路径 —— 不再重新分类（用户已经告诉我们了）
    out = _dispatch_forced(it, kind, text)
    tg.send(out.reply)

    if out.needs_ask:
        # ⚠️ 追问发出去了，就**不能**把待补充项清掉 —— 否则用户照着追问
        # 回一句「上午九点」时，系统已经没有上一条可接了，
        # 又会把它当成新的一条日程（标题变成「上午九点」）。
        #
        # 实测踩到过：端到端跑一遍，按钮这一步清了标记，下一步果然没接上。
        #
        # ⚠️ 这里也踩过一次"按钮绑错消息号"：槽位挪到追问那条消息，
        # 按钮却仍绑原消息号，于是点任何一个都只回"这条已经处理过了"，
        # 而追问文案还在说"点下面的按钮"。
        # 修法不是在两个 id 之间对齐（追问消息号要等发送返回才知道，
        # 很容易再错一次），而是让**按钮就解析当前槽位**（见 load_pending）。
        # 只有一个槽位，所以"当前那个"就是唯一答案，不需要 id 对齐。
        ask_msg_id = None
        try:
            r = tg.send_with_buttons("回一句时间就行：",
                                     _ask_keyboard(msg_id))
            ask_msg_id = r.get("message_id") if isinstance(r, dict) else None
        except Exception as e:  # noqa: BLE001
            _log(f"追问按钮发送失败（文字已发出，不影响）：{_trunc(str(e))}")

        # 槽位记下"追问那条消息号"只为排查方便；解析不依赖它
        save_pending(int(ask_msg_id) if ask_msg_id else msg_id, text)
        journal.append("button_resolved", text=text, kind=kind.value,
                       ok=out.ok, needs_more=True)
        return

    clear_pending(msg_id)
    journal.append("button_resolved", text=text, kind=kind.value, ok=out.ok)


def _dispatch_forced(it: Intake, kind: Kind, text: str):
    """
    按用户指定的类型分派（跳过分类）。

    日程缺时间的情况仍会返回"要求补充"—— 因为一个没有时间的日程
    在日历里没有意义，这时宁可让用户重说一句。
    """
    import classify
    import datetime as _dt
    c = classify.classify(text) or classify.Classified(
        kind, classify.Confidence.HIGH, text, text)
    c.kind = kind
    if kind is Kind.TODO:
        return it._do_todo(c)
    if kind is Kind.EVENT:
        return it._do_event(c)
    return it._do_memo(c)


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


def handle_message(text: str, msg_id: int, chat: str) -> None:
    _log(f"收到：{_trunc(text)!r}")
    it = _make_intake()

    # 把"上一条还在等补充的内容"传进去。
    #
    # 为什么需要：用户被追问"日程没写时间"时，最自然的回答是"上午九点"。
    # 那是一条没有内容的消息 —— 实测踩到：它被当成**新的一条日程**建出来，
    # 标题就是「上午九点」，而原来那条的事由丢了。
    #
    # 只有一个槽位，所以这里不需要判断"接哪一条"（见 PENDING 段的注释）。
    pending_text = load_pending()
    out = it.handle(text, msg_id=msg_id, pending_text=pending_text)

    reply = out.reply
    if _OFFLINE:
        calls = getattr(it, "_offline_calls", [])
        if calls:
            reply += "\n\n【离线模式】实际会写：" + "；".join(calls)
        else:
            reply += "\n\n【离线模式】不写入任何东西"
    tg.send(reply)

    if out.needs_ask:
        # 存下原文，按钮回调时取用 —— 这样按钮不依赖内存状态
        r = tg.send_with_buttons("选一个：", _ask_keyboard(msg_id))
        save_pending(msg_id, text)
        _log(f"已发出确认按钮（msg {r.get('message_id')}）")
    else:
        # ⚠️ 只在**本条成功接上/写成功**时才清掉槽位。
        # 失败时留着，用户还能照着再补一次 —— 清了就再也接不上了。
        if out.ok:
            clear_pending()
            if pending_text is not None and pending_text != text:
                _log(f"已用掉待补充项（原文 {_trunc(pending_text)!r}）")
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

        if "callback_query" in u:
            cb = u["callback_query"]
            m = cb.get("message") or {}
            chat = str(m.get("chat", {}).get("id", ""))
            try:
                _handle_callback(cb.get("data") or "", cb.get("id") or "", chat)
            except Exception as e:  # noqa: BLE001
                _log(f"按钮处理失败：{type(e).__name__}: {_trunc(str(e))}")
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
            continue
        try:
            handle_message(text, msg.get("message_id", 0), chat)
        except Exception as e:  # noqa: BLE001
            # 单条失败不能让守护进程崩 —— 崩了就漏掉后面所有消息
            _log(f"处理失败：{type(e).__name__}: {_trunc(str(e))}")
            try:
                tg.send(f"❌ 这条没处理成：{type(e).__name__}\n"
                        f"　　（你可以直接在对应 App 里手动加）")
            except Exception:  # noqa: BLE001
                pass

    save_offset(new_offset)
    return new_offset


# 启动通知的冷却时间（秒）。5 分钟内不重复发。
NOTIFY_COOLDOWN = 300


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
            tg.send("👋 pdca 收件守护已启动。直接发一句就行：\n"
                    "　　「明天交电费」→ 提醒事项\n"
                    "　　「周五下午两点项目周会」→ 日历\n"
                    "　　「想起一件事，…」→ 备忘录")
            _mark_notified()
        except tg.TelegramError as e:
            _log(f"启动通知发送失败（不影响运行）：{_trunc(str(e))}")
    else:
        _log("（启动通知处于冷却期，跳过 —— 避免反复重启时刷屏）")

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
