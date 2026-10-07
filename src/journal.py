#!/usr/bin/env python3
"""
用户日志：传感器读数（追加写入，永不修改）。

## 这个模块的定位（关键，别搞错）

journal **不是状态源**，是**传感器读数** —— 类似并联在电路中的电压表：

    ┌──────────────────┐
    │  Apple 应用       │ ← 状态源（唯一真相）
    └────────┬─────────┘
             │ 只读快照
             ▼
        journal（读数记录）

**它没有"当前状态"这个概念。** "某条备忘还在不在"永远从 Apple 应用现读，
journal 只回答"我提交过什么"和"我观察到什么"。

## 为什么不做镜像副本

曾经的方案是让 journal 保存一份待办的**镜像**，用它判断"该提醒什么"。
那有两个必然缺陷：

  ① 你在 Apple 应用里删掉一条 → 镜像不知道 → 若用它重建，会把删掉的造回来
  ② 镜像里那条还在 → 日报提醒你一件早已处理完的事

根因：镜像是**第二份状态**，两份一旦不同就得猜谁对 —— 回到"猜身份"老坑。
（用户的比喻很准：那是"左脚踩右脚"的死循环。）

## 两条铁律

| 机制 | 记什么 | 绝不用于 |
|---|---|---|
| 动作日志 | "我提交过什么" | 判断某条是否还存在 |
| 观察日志 | "我读到过什么" | 决定下次怎么写 |

读取路径只产生**观察记录**，不反馈进写入路径 —— 所以不构成闭环。

## 存储

    data/journal/YYYY-MM-DD.jsonl   一天一个文件，一行一条 JSON

**追加式**：只 append，从不修改已有行。这样即使程序出错，
历史也不会被破坏（可以审计"当时到底发生了什么"）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

# 项目根（本文件在 src/ 下）
ROOT = Path(__file__).resolve().parent.parent

# 用户日志目录。**支持环境变量覆盖**，这是给测试用的安全阀。
#
# 为什么需要：实测踩到 —— 我用 `daemon._OFFLINE = True` 验证离线模式时，
# 忘了把 JOURNAL_DIR 指到临时目录，于是 7 条测试记录直接写进了
# **真实的** data/journal/。后果不只是脏数据：日报的"防遗忘"读的就是
# 这份台账，污染会让它提醒一条根本不存在的事。
#
# 所以在测试里显式设：
#     PDCA_JOURNAL_DIR=$(mktemp -d) python3 ...
JOURNAL_DIR = Path(
    os.environ.get("PDCA_JOURNAL_DIR") or (ROOT / "data" / "journal")
)


class JournalContaminationError(RuntimeError):
    """测试代码试图往真实日志里写（防止污染用户数据）。"""


def assert_not_real(why: str = "") -> None:
    """
    断言当前写的**不是**真实日志目录。

    ⚠️ 这是给**测试**用的，不是给生产用的。
    曾经把它放进 append()（唯一的写入点），结果**把生产也拦住了** ——
    守护要写的正是真实目录，断言抛错后 `_journal` 又静默吞掉异常
    （"日志失败不影响主流程"），于是 journal 全空而任务显示 ok=True。
    实测踩到：真实消息被处理了，journal 里一条都没有。

    正解：**由调用方声明自己是不是测试**（Intake(testing=True)、
    离线模式），而不是由写入点猜。
    """
    real = (ROOT / "data" / "journal").resolve()
    try:
        cur = JOURNAL_DIR.resolve()
    except OSError:
        return
    if cur == real and not os.environ.get("PDCA_ALLOW_REAL_JOURNAL"):
        raise JournalContaminationError(
            f"拒绝写入真实日志目录（{why or '未说明原因'}）。\n"
            f"测试请设 PDCA_JOURNAL_DIR 指向临时目录；\n"
            f"确实要写真实日志时设 PDCA_ALLOW_REAL_JOURNAL=1。")


def is_real_dir() -> bool:
    """当前是否指向真实日志目录（供自检判断）。"""
    try:
        return JOURNAL_DIR.resolve() == (ROOT / "data" / "journal").resolve()
    except OSError:
        return False

# 事件类型。用常量避免拼错 —— 拼错的事件类型会静默地永远匹配不上。
EV_INPUT = "input"            # 你的原始输入（原样保存，便于复盘）
EV_TODO_ADDED = "todo_added"
EV_EVENT_ADDED = "event_added"
EV_MEMO_ADDED = "memo_added"
EV_MEMO_CLEARED = "memo_cleared"
EV_OBSERVED = "observed"      # 一次快照观察的结果
EV_ERROR = "error"            # 处理失败（供排查）
EV_DIGEST_PUSHED = "digest_pushed"   # 日报投递结果（含每通道成败与心跳结果）
EV_DIGEST_MISSING = "digest_missing"  # 到点没送到 → 看门狗发过一次告警

# 会用到的日期键（某些事件跨天出现，需要单独记）
DATE_KEY = "for_date"


def _today() -> str:
    return dt.date.today().isoformat()


def _path_for(date_str: str) -> Path:
    return JOURNAL_DIR / f"{date_str}.jsonl"


def now_iso() -> str:
    """本地时间的 ISO 字符串（带时区偏移，便于日后跨时区核对）。"""
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def append(event: str, **fields) -> dict:
    """
    追加一条记录。返回写入的那条（便于调用方回显/测试）。

    ⚠️ 这个函数**只追加**，不会读取或修改任何已有内容 ——
    这是"传感器"定位的直接体现。哪怕传入的数据是错的，
    也只是多一条错记录，不会破坏历史。

    **污染防护不在这里** —— 由调用方声明自己是不是测试
    （见 assert_not_real 的说明：放在写入点会把生产也拦住）。
    """
    rec = {"at": now_iso(), "event": event}
    rec.update(fields)

    date_str = fields.get(DATE_KEY) or _today()
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = _path_for(date_str)

    # 一行一条 JSON（ensure_ascii=False 保留中文可读性；
    # separators 去掉多余空格，文件更小也更好肉眼扫）
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())      # 立刻落盘：日志丢了就找不回来了

    return rec


# ── 便捷包装（让调用处读起来像业务语言，而不是一堆字符串）

def log_input(text: str, source: str = "telegram", msg_id: int | None = None) -> dict:
    """记下你的原始输入。**原样保存**，不做任何加工。"""
    return append(EV_INPUT, text=text, source=source, msg_id=msg_id)


def log_todo(text: str, reminder_id: str = "", ok: bool = True,
             detail: str = "", due: str = "", allday: bool = False,
             flagged: bool = False, priority: int = 0,
             src_msg_id: int | None = None,
             reply_msg_id: int | None = None) -> dict:
    """
    记下"我建了一条待办"。

    `reply_msg_id` 是**我发出的那条回执**的 message_id ——
    有了它，用户"回复我的回执"时才能精确定位到这一条（见 resolve_prev）。
    没有它就只能靠"最近一条"猜，而猜身份是本项目踩过四次的坑。

    `due` / `allday` / `flagged` / `priority`（**2026-10-07 补**）＝ 这条待办
    落成的**原生字段**。为什么不靠读回来答：日报只读
    name / completed / body / 完成时刻，**不读**到期日与旗标 ——
    所以"我当时到底设了什么"只有这里记得下来。
    """
    return append(EV_TODO_ADDED, text=text, reminder_id=reminder_id,
                  ok=ok, detail=detail, due=due, allday=allday,
                  flagged=flagged, priority=priority,
                  src_msg_id=src_msg_id, reply_msg_id=reply_msg_id)


def log_event(summary: str, start: str = "", end: str = "",
              location: str = "", calendar: str = "", ok: bool = True,
              detail: str = "", recurrence: str = "", alarm: bool = False,
              src_msg_id: int | None = None,
              reply_msg_id: int | None = None) -> dict:
    """
    记下"我建了一条日程"。

    `recurrence` 是 iCal RRULE（空 = 一次性）。**2026-10-05 补上**：
    那天想核对"用户发的那条英语学习到底是不是每天重复"，
    发现 journal 里记了标题和时间、**唯独没记规则** ——
    于是这个"我们提交了什么"的问题只能靠猜（读路径展开也要靠这个字段）。

    `alarm` = 这条日程**有没有设闹钟**（**2026-10-07 补**，同 `recurrence` 的理由）：
    "到点会不会响"属于"我们提交了什么"，而它只存在于**创建那一刻** ——
    读路径（`applecal.events_between`）**不返回闹钟信息**，
    所以不记下来就再也查不回来。
    """
    return append(EV_EVENT_ADDED, summary=summary, start=start, end=end,
                  location=location, calendar=calendar, ok=ok, detail=detail,
                  recurrence=recurrence, alarm=alarm,
                  src_msg_id=src_msg_id, reply_msg_id=reply_msg_id)


def log_memo(text: str, memo_id: str, ok: bool = True, detail: str = "",
             src_msg_id: int | None = None,
             reply_msg_id: int | None = None) -> dict:
    """
    记下"我提交过一条备忘"。

    `memo_id` 是 Apple 分配的笔记 id —— 它是**观察阶段的唯一凭据**：
    下次只读快照时，靠它判断"我写的那条还在不在"。
    """
    return append(EV_MEMO_ADDED, memo_id=memo_id, text=text, ok=ok,
                  detail=detail, src_msg_id=src_msg_id,
                  reply_msg_id=reply_msg_id)


def log_memo_cleared(memo_id: str, text: str = "") -> dict:
    """记下"快照里发现这条不在了"（即你已删除）。此后永不再提醒。"""
    return append(EV_MEMO_CLEARED, memo_id=memo_id, text=text)


def log_observed(kind: str, present: int = 0, cleared: list | None = None,
                 note: str = "") -> dict:
    """记下一次观察（快照读到了什么、与台账的差集是什么）。"""
    return append(EV_OBSERVED, kind=kind, present=present,
                  cleared=cleared or [], note=note)


def log_error(where: str, detail: str) -> dict:
    return append(EV_ERROR, where=where, detail=detail)


def log_digest_pushed(results: list, digest_date: str = "",
                      generated_at: str = "", schedule_offset: int | None = None,
                      heartbeat: str = "", digest_kind: str = "daily") -> dict:
    """
    记下一次产物投递：**每通道**成败 + 本次是否偏离计划时间 + 心跳结果。

    为什么要记进 journal 而不是只 print 到 logs/：
    logs/ 是程序日志（README 明说"可随时清"），而"这份日报到底送到没有"
    属于**读数**，丢了就再也查不出来 —— 所以它该落在不可再生的 journal 里。

    `results` 形如 [("telegram", True, "已发送（message_id=93）"), ...]。
    `schedule_offset` 是相对计划时间的分钟数（正=晚、负=早、None=不适用）。

    ⚠️ `digest_kind` 是 2026-10-05 加的（周报落地时）：**必须区分是哪份产物**。
    在它之前，"这一天送到没有"只看有没有 `digest_pushed` —— 于是周日 20:00 的
    周报会把当天的**日报**标记成"已送达"，看门狗 23:30 检查时就不会告警，
    而那天 21:30 的日报根本没跑。那会让看门狗唯一的职责静默失效。
    老记录没有这个字段 → 一律当 `daily`（向后兼容，见 digest_kind_of）。
    """
    rec = append(
        EV_DIGEST_PUSHED,
        digest_date=digest_date,
        digest_kind=digest_kind,
        generated_at=generated_at,
        schedule_offset=schedule_offset,
        heartbeat=heartbeat,
        channels=[{"name": str(name), "ok": bool(ok), "detail": str(detail)}
                  for name, ok, detail in results],
        **({DATE_KEY: digest_date} if digest_date else {}),
    )
    return rec


def digest_kind_of(rec: dict) -> str:
    """
    这条投递读数是哪份产物。**缺字段 = "daily"**。

    这个默认值是刻意的：`digest_kind` 是后来加的字段，
    之前写下的记录全是日报 —— 把它们当成"未知"会让历史读数凭空消失。
    """
    return str(rec.get("digest_kind") or "daily")


# ── 纠正：把"用户指的是哪一条"解析出来
#
# 两条路径（见 docs/ARCHITECTURE.md 的"纠正"一节）：
#   A. 用户**回复我的回执** → 用 reply_to_message.message_id 精确定位（零猜测）
#   B. 用户直接发短指令   → 落到"最近一条"，但**必须先回显确认**再动
#
# 关键：A 路径完全不需要猜，所以优先；B 路径是便利性补充。

# 这些事件代表"确实往 Apple 应用里写了一条东西"，可被纠正
CORRECTABLE = (EV_TODO_ADDED, EV_EVENT_ADDED, EV_MEMO_ADDED)


def resolve_prev(reply_to_msg_id: int | None = None,
                 days: int = 2) -> dict | None:
    """
    找出用户想纠正的那一条。

    `reply_to_msg_id` 给定时（用户回复了某条消息）：
      先按"我发出的回执 id"匹配；匹配不到再按"用户原始消息 id"匹配
      （用户也可能回复自己的消息）。
      两者都没有 → 返回 None（**不猜**）。

    未给定时：返回**最近一条**可纠正记录（路径 B），
    此时调用方必须先回显确认。
    """
    recs: list[dict] = []
    for i in range(days):
        d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        recs.extend(read_day(d))
    acts = [r for r in recs if r.get("event") in CORRECTABLE]

    if reply_to_msg_id is not None:
        for r in reversed(acts):
            if r.get("reply_msg_id") == reply_to_msg_id:
                return r
        for r in reversed(acts):
            if r.get("src_msg_id") == reply_to_msg_id:
                return r
        return None          # 明确：不猜

    return acts[-1] if acts else None


# ── 读取（供复盘与"我提交过什么"）

def read_day(date_str: str) -> list[dict]:
    """读某天的全部记录。文件不存在时返回空列表（不是错误）。"""
    path = _path_for(date_str)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # 单行坏了不该让整个日志读不出来
            continue
    return out


def read_range(days: int, end: str | None = None) -> list[dict]:
    """读最近 `days` 天的记录（含当天），按时间先后。"""
    end_date = dt.date.fromisoformat(end) if end else dt.date.today()
    out: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = (end_date - dt.timedelta(days=i)).isoformat()
        out.extend(read_day(d))
    return out


def submitted_memos(days: int = 365, end: str | None = None) -> dict[str, dict]:
    """
    从日志里重建「我提交过哪些备忘」的台账。

    返回 {memo_id: {text, at, for_date}} —— **只含尚未观察到被清除的**。

    注意这**不是**"当前状态"，而是"我提交过、还没观察到你删除的"。
    "它现在还在不在"必须由只读快照回答（见 memo.py）。
    这个区分是整个架构的关键，别把两者混起来用。
    """
    added: dict[str, dict] = {}
    cleared: set[str] = set()

    for rec in read_range(days, end):
        ev = rec.get("event")
        mid = rec.get("memo_id")
        if not mid:
            continue
        if ev == EV_MEMO_ADDED:
            added[mid] = {"text": rec.get("text", ""), "at": rec.get("at", ""),
                          "for_date": rec.get("for_date") or rec.get("at", "")[:10]}
        elif ev == EV_MEMO_CLEARED:
            cleared.add(mid)

    return {k: v for k, v in added.items() if k not in cleared}


def log_digest_missing(day: str, detail: str = "", channel: str = "") -> dict:
    """
    记下"这一天到点没送到，看门狗已经告警过一次"。

    这条记录有两个作用：
      · 看门狗靠它**每天最多告警一次**（不引入新的状态文件）；
      · 复盘时能看出"哪几天真的漏了"（digest_pushed 里没有的那些天）。
    """
    return append(EV_DIGEST_MISSING, digest_date=day, detail=detail,
                  channel=channel, **{DATE_KEY: day})


def digest_dates(days: int = 30, end: str | None = None,
                 kind: str = "daily") -> list[str]:
    """
    最近这些天里，**确实投递过**这份产物的日期（升序、去重）。

    这是"读数里有哪些天送出去了"，不是状态源 —— 与 submitted_memos 同一性质。

    ⚠️ `kind` 默认 `daily`：看门狗问的是"**日报**送到没有"，
    而周日的周报投递**不能**算作当天的日报（否则看门狗会静默失效 ——
    见 log_digest_pushed 的说明）。
    """
    seen = set()
    for rec in read_range(days, end):
        if rec.get("event") != EV_DIGEST_PUSHED:
            continue
        if digest_kind_of(rec) != kind:
            continue
        day = rec.get("digest_date") or str(rec.get("at", ""))[:10]
        if day:
            seen.add(str(day))
    return sorted(seen)


def delivered_on(day: str, kind: str = "daily") -> bool:
    """这一天有没有该产物投递成功的读数（供看门狗判断"今天日报送到没有"）。"""
    return day in digest_dates(days=1, end=day, kind=kind)


def alerted_on(day: str) -> bool:
    """这一天是否已经因"没送到"告警过（供看门狗做每日一次的冷却）。"""
    return any(rec.get("event") == EV_DIGEST_MISSING
               for rec in read_day(day))


def channel_health(days: int = 14, end: str | None = None,
                   kind: str = "daily") -> dict[str, dict]:
    """
    从 `digest_pushed` 读数里汇总**每通道最近一次成功**与**连续失败天数**。

    ⚠️ 与 submitted_memos 同类：这不是"当前状态"，而是**读数的汇总**。
    它回答的是"我上一次把**日报**送出去是什么时候"，用于在日报里提示
    "某个通道已经好几天没成功了"（否则单通道静默失效可以瞒你几个月）。

    同一天多次投递（手工重跑）按"任一次成功即算当天成功"合并 ——
    失败重试成功不该被记为失败。

    ⚠️ `kind` 默认 `daily`（2026-10-05 加）：这份读数问的是**日报通道**的健康。
    把周报（每周只有一条）混进来会让"连续失败天数"被一次周报成功清零，
    从而掩盖日报的连续失败 —— 那是这条读数唯一要发现的东西。
    """
    per_day: dict[str, dict[str, bool]] = {}
    last_ok: dict[str, str] = {}
    seen: dict[str, str] = {}
    last_detail: dict[str, str] = {}

    for rec in read_range(days, end):
        if rec.get("event") != EV_DIGEST_PUSHED:
            continue
        if digest_kind_of(rec) != kind:
            continue
        day = rec.get("digest_date") or str(rec.get("at", ""))[:10]
        at = str(rec.get("at", ""))
        for ch in rec.get("channels") or []:
            name = str(ch.get("name", ""))
            if not name:
                continue
            ok = bool(ch.get("ok"))
            seen.setdefault(name, at)
            seen[name] = at
            last_detail[name] = str(ch.get("detail", ""))
            day_map = per_day.setdefault(day, {})
            day_map[name] = day_map.get(name, False) or ok
            if ok:
                last_ok[name] = at

    today = dt.date.fromisoformat(end) if end else dt.date.today()
    out: dict[str, dict] = {}
    for name in seen:
        # 连续失败天数：从最后一天往前数，直到遇到"这天成功"
        streak = 0
        for i in range(0, max(days, 1)):
            d = (today - dt.timedelta(days=i)).isoformat()
            st = per_day.get(d, {}).get(name)
            if st is None:
                continue          # 那天没有读数（还没跑），不计入也不中断
            if st:
                break
            streak += 1
        out[name] = {
            "last_ok": last_ok.get(name, ""),
            "consecutive_fail_days": streak,
            "days_since_ok": _days_since(last_ok.get(name, ""), today),
            "last_detail": last_detail.get(name, ""),
        }
    return out


def _days_since(iso_at: str, today: dt.date) -> int | None:
    """某个 ISO 时刻距今天几天。解析不了返回 None（**不猜**）。"""
    if not iso_at:
        return None
    try:
        delta = (today - dt.datetime.fromisoformat(iso_at).date()).days
    except ValueError:
        return None
    return max(delta, 0)      # 时区/补写可能算出负数，负数没有意义


def main() -> int:
    """命令行自检：追加一条测试记录并读回。"""
    import argparse

    ap = argparse.ArgumentParser(description="用户日志（传感器读数）")
    ap.add_argument("--show", metavar="DATE", nargs="?", const="today",
                    help="打印某天的记录（默认今天）")
    ap.add_argument("--test", action="store_true", help="追加一条测试记录")
    args = ap.parse_args()

    if args.test:
        rec = log_input("这是一条测试输入（可忽略）", source="selftest")
        print(f"✅ 已追加：{json.dumps(rec, ensure_ascii=False)}")
        print(f"   文件：{_path_for(_today()).relative_to(ROOT)}")
        return 0

    date_str = _today() if args.show in (None, "today") else args.show
    recs = read_day(date_str)
    print(f"{date_str}：{len(recs)} 条记录")
    print(f"文件：{_path_for(date_str).relative_to(ROOT)}")
    for r in recs:
        ev = r.get("event", "?")
        rest = {k: v for k, v in r.items() if k not in ("event", "at")}
        print(f"  {r.get('at','')[:19]}  {ev:<14} {json.dumps(rest, ensure_ascii=False)[:80]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
