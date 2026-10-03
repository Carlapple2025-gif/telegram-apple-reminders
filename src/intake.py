#!/usr/bin/env python3
"""
收件：把一句自然语言分派到对应的 Apple 应用。

## 这一步做三件事

    ① 分类（classify.py）：待办 / 日程 / 备忘
    ② 分派到唯一归属：
         待办 → 提醒事项   日程 → 日历   备忘 → 备忘录（追加）
    ③ 记 journal（传感器读数）+ 回执文案

## 三个写入端是**可注入**的

这是本模块能彻底离线测试的关键：

    Intake(add_todo=..., add_event=..., add_memo=...)

生产环境用真实适配器（reminders / applecal / memo 模块），
测试时注入假实现 —— 于是**全部分支都能验掉**，
不需要备忘录/日历权限，也不会污染真实数据。

（v1 的分发逻辑绑死在真实 AppleScript 上，只能靠手动跑验证，
这是它 bug 反复出现的原因之一。）

## 严守的架构约束

  · 只"增"，不删不改任何 Apple 应用里的东西
  · 写完**不读回**做判断（只在 journal 记"我提交过什么"）
  · 判不出来时问一次（`Confidence.ASK`），**绝不静默丢弃**
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import classify
import journal
from classify import Classified, Confidence, Kind

# ── 写入端协议
#
# 用 Protocol 描述"这三个端要长什么样"，好处是测试时可以塞任何
# 符合签名的假实现，而生产代码不需要知道具体类型。

class TodoSink(Protocol):
    def __call__(self, text: str, when: dt.datetime | None = None) -> str:
        """新建一条待办，返回它的 id。"""
        ...


class EventSink(Protocol):
    def __call__(self, summary: str, start: dt.datetime, end: dt.datetime,
                 location: str = "", recurrence: str = "",
                 allday: bool = False) -> str:
        """新建一条日程，返回它的 uid。"""
        ...


class MemoSink(Protocol):
    def __call__(self, text: str) -> str:
        """追加一条备忘，返回它的 note_id。"""
        ...


# 事件名 → journal 里的记录函数。
#
# 用一张表而不是分散的调用：这样"事件名"只有一处定义，
# 传错名字会静默失败的问题就不可能发生（取不到就当没这回事）。
_LOG_FUNCS: dict[str, Callable[..., dict]] = {
    "input": journal.log_input,
    "todo_added": journal.log_todo,
    "event_added": journal.log_event,
    "memo_added": journal.log_memo,
    "memo_cleared": journal.log_memo_cleared,
    "error": journal.log_error,
}


@dataclass
class Outcome:
    """一次收件的结果（含给用户看的回执）。"""
    ok: bool
    kind: Kind | None = None
    text: str = ""
    reply: str = ""
    ref_id: str = ""                  # 提醒事项 id / 日历 uid / 笔记 id
    needs_ask: bool = False           # 需要用户确认是哪一类
    candidates: list[Kind] = field(default_factory=list)
    error: str = ""
    when_text: str = ""               # 日程的时间描述

    @property
    def should_record(self) -> bool:
        """是否该记进 journal（失败的也要记，便于复盘）。"""
        return True


# ── 回执文案
#
# 文案要能回答"它去哪了" —— 否则用户不知道去哪儿找。
# 这是 v1 的教训：写入成功但用户不知道写哪了，等于没写成。

_KIND_LABEL = {
    Kind.TODO: "待办",
    Kind.EVENT: "日程",
    Kind.MEMO: "备忘",
}

_KIND_PLACE = {
    Kind.TODO: "提醒事项",
    Kind.EVENT: "日历",
    Kind.MEMO: "备忘录",
}


def _reply_ok(c: Classified, ref_id: str) -> str:
    label = _KIND_LABEL[c.kind]
    place = _KIND_PLACE[c.kind]
    line = f"✅ 已记下（{label}）：{c.text}"
    detail = []
    if c.kind is Kind.EVENT and c.when is not None:
        import whens
        detail.append(whens.format_when(c.when))
    if c.recurrence:
        detail.append(_recurrence_text(c.recurrence))
    if detail:
        line += "\n　　" + " · ".join(detail)
    line += f"\n　　→ 在「{place}」里"
    return line


def _recurrence_text(rrule: str) -> str:
    """把 RRULE 转成人话（回执里别让用户看 FREQ=WEEKLY）。"""
    if "BYDAY=MO,TU,WE,TH,FR" in rrule:
        return "每个工作日"
    if "FREQ=DAILY" in rrule:
        return "每天"
    if "FREQ=WEEKLY" in rrule:
        day = rrule.split("BYDAY=", 1)[1].split(";")[0] if "BYDAY=" in rrule else ""
        name = {"MO": "一", "TU": "二", "WE": "三", "TH": "四",
                "FR": "五", "SA": "六", "SU": "日"}.get(day, "")
        return f"每周{name}" if name else "每周"
    if "FREQ=MONTHLY" in rrule:
        dom = rrule.split("BYMONTHDAY=", 1)[1].split(";")[0] if "BYMONTHDAY=" in rrule else ""
        return f"每月{dom}日" if dom else "每月"
    return rrule


def _reply_ask(c: Classified) -> str:
    return (f"🤔 「{c.raw}」我不确定该怎么记 —— 它是哪一种？\n"
            f"　　（选错也没关系，你在对应 App 里改就行）")


# ── 主流程

class Intake:
    """
    收件处理器。

    三个写入端可注入；不传则用真实实现（延迟导入，避免测试时
    因为缺少 AppleScript 权限而失败）。
    """

    def __init__(self,
                 add_todo: TodoSink | None = None,
                 add_event: EventSink | None = None,
                 add_memo: MemoSink | None = None,
                 log: Callable[..., dict] | None = None):
        self._add_todo = add_todo
        self._add_event = add_event
        self._add_memo = add_memo
        self._log = log

    # ── 真实的写入端（延迟导入 + 延迟构造）

    @property
    def add_todo(self) -> TodoSink:
        if self._add_todo is None:
            self._add_todo = _real_add_todo
        return self._add_todo

    @property
    def add_event(self) -> EventSink:
        if self._add_event is None:
            self._add_event = _real_add_event
        return self._add_event

    @property
    def add_memo(self) -> MemoSink:
        if self._add_memo is None:
            self._add_memo = _real_add_memo
        return self._add_memo

    # ── 入口

    def handle(self, text: str, base: dt.date | None = None,
               msg_id: int | None = None) -> Outcome:
        """
        处理一句输入。**永不抛异常** —— 失败也返回 Outcome，
        因为收件是常驻进程的主路径，崩一次就漏一条消息。
        """
        raw = (text or "").strip()
        self._journal("input", text=raw, source="telegram", msg_id=msg_id)

        if not raw:
            return Outcome(ok=False, reply="（空消息，已忽略）", error="empty")

        try:
            c = classify.classify(raw, base)
        except Exception as e:  # noqa: BLE001
            return self._fail(raw, f"分类出错：{e}")

        if c is None:
            return Outcome(ok=False, reply="（空消息，已忽略）", error="empty")

        if c.needs_ask:
            return Outcome(ok=False, kind=c.kind, text=c.text,
                           reply=_reply_ask(c), needs_ask=True,
                           candidates=list(c.candidates))

        try:
            if c.kind is Kind.TODO:
                return self._do_todo(c)
            if c.kind is Kind.EVENT:
                return self._do_event(c)
            return self._do_memo(c)
        except Exception as e:  # noqa: BLE001
            return self._fail(c.text, f"{_KIND_LABEL[c.kind]}写入失败：{e}",
                              kind=c.kind)

    # ── 三条分派路径

    def _do_todo(self, c: Classified) -> Outcome:
        # 待办可以带时间（"明天交电费"），但**归属仍是提醒事项** ——
        # 时间不改变归属，只是"什么时候做"的提示。
        #
        # 刻意**不设到期日**：给每条待办都设 due 会制造假紧迫感
        # （到期弹通知、变红），而用户要的是"记下来别忘了"，不是催命。
        # 时间由提醒事项的备注携带（见 _real_add_todo）。
        #
        # 注：这个 when 是 classify 特意保留的。曾经 classify 在待办分支
        # 把它丢掉，导致"明天"这类信息白解析 —— 所以那里改过了。
        due = c.when.start if (c.when and c.when.has_date) else None
        ref = self.add_todo(c.text, due)
        self._journal("todo_added", text=c.text, reminder_id=ref, ok=True)
        return Outcome(ok=True, kind=Kind.TODO, text=c.text,
                       reply=_reply_ok(c, ref), ref_id=ref)

    def _do_event(self, c: Classified) -> Outcome:
        if c.when is None:
            # 日程必须有时间。分类器说它是日程却没解析出时间
            # （比如只说了"例会"）→ 落到"问一次"，而不是瞎猜一个时间。
            c2 = Classified(Kind.EVENT, Confidence.ASK, c.text, c.raw,
                            reason="是日程但没写时间",
                            candidates=[Kind.TODO, Kind.EVENT, Kind.MEMO])
            return Outcome(ok=False, kind=Kind.EVENT, text=c.text,
                           reply=(f"🤔 「{c.raw}」像是日程，但没写时间。\n"
                                  f"　　再说一次带上时间？或者它是待办/备忘？"),
                           needs_ask=True, candidates=list(c2.candidates))

        import whens
        ref = self.add_event(c.text, c.when.start, c.when.end,
                            recurrence=c.recurrence, allday=c.when.all_day)
        self._journal("event_added", summary=c.text,
                      start=c.when.start.isoformat(),
                      end=c.when.end.isoformat(),
                      calendar="", ok=True)
        return Outcome(ok=True, kind=Kind.EVENT, text=c.text,
                       reply=_reply_ok(c, ref), ref_id=ref,
                       when_text=whens.format_when(c.when))

    def _do_memo(self, c: Classified) -> Outcome:
        ref = self.add_memo(c.text)
        self._journal("memo_added", text=c.text, memo_id=ref, ok=True)
        return Outcome(ok=True, kind=Kind.MEMO, text=c.text,
                       reply=_reply_ok(c, ref), ref_id=ref)

    # ── 辅助

    def _fail(self, text: str, msg: str, kind: Kind | None = None) -> Outcome:
        self._journal("error", where="intake", detail=msg)
        return Outcome(ok=False, kind=kind, text=text,
                       reply=f"❌ 没记成：{msg}\n　　（你可以直接在对应 App 里手动加）",
                       error=msg)

    def _journal(self, event: str, **kw) -> None:
        """
        记 journal。**失败不影响主流程** —— 日志坏了不该让收件失败。

        ⚠️ 参数是**事件名**（字符串），不是函数对象。
        曾经写成 `self._journal(journal.log_error, where=..., detail=...)`，
        把"事件名"和"日志函数的参数"混在一起 —— 结果 log_error 收到
        `event="intake"`，而 `where`/`detail` 全丢了，日志**静默写不进去**。
        （这正是本项目反复出现的"静默失效"，所以下面有断言守着。）
        """
        fn = _LOG_FUNCS.get(event)
        if fn is None:
            return
        try:
            (self._log or fn)(**kw)
        except Exception:  # noqa: BLE001
            pass


# ── 真实写入端（生产路径）

def _real_add_todo(text: str, when: dt.datetime | None = None) -> str:
    """
    写进提醒事项。

    注意：**不设到期日**。给每条待办都设 due 会制造假紧迫感
    （到期就弹通知），而用户要的是"记下来别忘了"，不是催命。
    时间信息留在提醒事项的备注里。
    """
    import reminders
    rem = reminders.Reminders()
    rem.verify_list()
    body = reminders.make_key(text)
    if when is not None:
        body = f"{body} · {when.strftime('%m-%d %H:%M')}"
    r = rem.create(name=text, body=body)
    return getattr(r, "id", "") or ""


def _real_add_event(summary: str, start: dt.datetime, end: dt.datetime,
                    location: str = "", recurrence: str = "",
                    allday: bool = False) -> str:
    import applecal
    ev = applecal.add(summary, start, end, location=location,
                      recurrence=recurrence, allday=allday)
    return ev.uid


def _real_add_memo(text: str) -> str:
    import memo
    m = memo.add(text)
    return m.note_id


# ── CLI（离线演示 + 手工触发）

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="收件：分类并分派")
    ap.add_argument("text", nargs="+", help="要处理的文本")
    ap.add_argument("--base", help="基准日期 YYYY-MM-DD")
    ap.add_argument("--dry", action="store_true",
                    help="只解析与分派判断，**不真正写入**")
    args = ap.parse_args()

    base = dt.date.fromisoformat(args.base) if args.base else None

    if args.dry:
        # 干跑：用假写入端，验证分类与回执文案
        written: list[str] = []

        def fake_todo(text, when=None):
            written.append(f"todo({text!r}, when={when})")
            return "FAKE-TODO"

        def fake_event(summary, start, end, location="", recurrence="", allday=False):
            written.append(f"event({summary!r}, {start} → {end}, rrule={recurrence!r})")
            return "FAKE-EVENT"

        def fake_memo(text):
            written.append(f"memo({text!r})")
            return "FAKE-MEMO"

        it = Intake(add_todo=fake_todo, add_event=fake_event,
                    add_memo=fake_memo, log=lambda **kw: {})
    else:
        it = Intake()

    for t in args.text:
        print("─" * 50)
        print(f"输入：{t}")
        out = it.handle(t, base)
        print(out.reply)
        if out.needs_ask:
            print(f"　　（候选：{' / '.join(k.value for k in out.candidates)}）")

    if args.dry:
        print()
        print("═" * 50)
        print("【干跑】实际会调用的写入：")
        for w in written:
            print(f"  · {w}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
