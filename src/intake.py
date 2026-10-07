#!/usr/bin/env python3
"""
收件：把一条输入写进对应的 Apple 应用。

## 这一步做三件事

    ① 路由（routes.py）：**按行首符号**决定归属 —— 不猜
    ② 写入唯一归属：
         待办 → 提醒事项   日程 → 日历   备忘 → 备忘录（追加）
    ③ 记 journal（传感器读数）+ 回执文案

**"判断"不在这里，也不在代码里** —— 类型由你写的符号声明
（见 [`docs/SYMBOL-SCHEME.md`](../docs/SYMBOL-SCHEME.md)）。
本模块只负责"把已经定型的条目写进去"。

> 2026-10-03 之前，这里调用 `classify.py` 从自然语言**推断**类型
> （约 700 字词表 + 405 行规则）。那天 4 次真实交互错了 3 次，
> 于是整条推断链路被删除：**猜错会静默写错 App，
> 而声明的错误是看得见的**。

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
  · 失败**如实报错**，绝不静默丢弃（也不追问 —— 见 routes.py 的说明）
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import journal
import routes
from kinds import Item, Kind
from routes import RouteError

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
                 allday: bool = False, alarm: bool = False) -> str:
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
    error: str = ""
    when_text: str = ""               # 日程的时间描述

    # 注：这里曾有 `needs_ask` / `candidates` 两个字段 ——
    # "判不出来就问一次"。追问机制已整体删除（2026-10-03 裁决）：
    # 它要跨消息记住"在等什么"，命中 ARCHITECTURE §十一 判据 1。
    # 类型现在由符号声明，判不出来的情况不存在。

    @property
    def should_record(self) -> bool:
        """是否该记进 journal（失败的也要记，便于复盘）。"""
        return True


# ── 回执文案
#
# 文案要能回答"它去哪了" —— 否则用户不知道去哪儿找。
# 这是 v1 的教训：写入成功但用户不知道写哪了，等于没写成。
#
# 两条约束（2026-10-04，见 docs/TELEGRAM-VOICE.md V6）：
#   · **不复述全文**：一条 100 字的感慨被原样回声，聊天记录会变成半屏回声。
#     正文一个字不少地写进了 App，回执只需要够你认出"是这条"。
#   · **待办也要显示时间**：否则你给了"明天"，回执里看不到，
#     只能打开 App 才知道记没记上（而且必须说明它不会到期提醒）。

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

# 回执里引用正文的上限（字符）。超过截断并加省略号。
ECHO_MAX = 20


def _echo(text: str) -> str:
    """回执里引用正文：压掉换行（回执要保持单行好认）、超长截断。"""
    t = " ".join((text or "").split())
    return t if len(t) <= ECHO_MAX else t[:ECHO_MAX] + "…"


def _alarm_for(it: Item) -> bool:
    """
    这条日程要不要设闹钟。

    **只给带重复规则的日程**（2026-10-07 用户裁决）：

    - 一次性日程你本来就知道那天有事；
    - "每天/每周要做的事"没有闹钟就等于没提醒 —— 而提醒事项的脚本接口
      **没有重复**（见 `docs/APPLE-FACTS.md` §2.1），
      所以"每天 8:35 提醒我"只有日历做得到。

    判定放这里、"闹钟在日历里怎么表达"（定时=开始时 / 全天=当天 09:00）
    放 `applecal.add` —— 一个是产品决定，一个是平台细节，不混在一起。
    """
    return bool(it.recurrence)


def _reply_ok(it: Item, ref_id: str) -> str:
    import whens

    label = _KIND_LABEL[it.kind]
    place = _KIND_PLACE[it.kind]
    line = f"✅ 已记下（{label}）：{_echo(it.text)}"
    detail = []
    if it.kind is Kind.EVENT and it.when is not None:
        detail.append(whens.format_when(it.when))
    elif it.kind is Kind.TODO and it.when is not None:
        # 待办**不设到期日**（见 _real_add_todo）：时间只写进备注。
        # 所以必须把这件事说出来 —— 否则"10月5日 周一"看起来像会到期通知。
        detail.append(f"{whens.format_when(it.when, all_day_note=False)}"
                      f"（只写进备注，不会到期提醒）")
    if it.recurrence:
        detail.append(whens.rrule_text(it.recurrence))
    if it.kind is Kind.EVENT and _alarm_for(it):
        # 放**最后**，读起来是"时间 · 每天 · 到点会提醒"。
        # 回执必须说清这次会不会响 —— 与待办那句"不会到期提醒"对称；
        # 2026-10-07 的真实困惑就是"写进去了"但到点不响、而回执一个字没提。
        detail.append("到点会提醒")
    if detail:
        line += "\n　　" + " · ".join(detail)
    line += f"\n　　→ 在「{place}」里"
    return line




# 注：这里曾有 `_recurrence_text()`（RRULE → 人话）。2026-10-05 搬到
# `whens.rrule_text()` —— 因为**读取路径也要用它**（重复日程按天出现时
# 要标出"这条是每天的"），而"RRULE ↔ 人话"属于时间词汇，归 whens。
# 留着两份的代价是：改了一处、另一处不变（本项目反复踩过的那个坑）。


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
                 log: Callable[..., dict] | None = None,
                 testing: bool = False):
        self._add_todo = add_todo
        self._add_event = add_event
        self._add_memo = add_memo
        self._log = log
        # testing=True 时断言"没在往真实 journal 写" —— 防止测试数据
        # 污染真实台账（日报的"防遗忘"读的就是它）。
        # 生产不要设这个：守护要写的正是真实目录。
        if testing:
            journal.assert_not_real("Intake(testing=True)")

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

        ⚠️ 这里**没有** `pending_text` 参数了。它曾用于"用户回答追问、
        要接到上一条上"—— 而那套追问机制已整体删除（2026-10-03 裁决）：
        追问要跨消息记住"在等什么"，命中 `ARCHITECTURE.md` §十一 判据 1。
        现在缺什么就带符号重发一条完整的。
        """
        raw = (text or "").strip()
        self._journal("input", text=raw, source="telegram", msg_id=msg_id)

        if not raw:
            return Outcome(ok=False, reply="（空消息，已忽略）", error="empty")

        # ① 路由：按符号决定归属。**不猜** —— 判据是符号，不是语义。
        try:
            it = routes.route(raw, base)
        except RouteError as e:
            # 载荷有硬问题（日程没时间/只有时间/只有符号）。
            # 如实报错并**不写入** —— 不退回别的类型、不猜一个时间。
            return self._fail(raw, str(e))
        except Exception as e:  # noqa: BLE001
            return self._fail(raw, f"路由出错：{e}")

        # ② 写入
        try:
            if it.kind is Kind.TODO:
                return self._do_todo(it)
            if it.kind is Kind.EVENT:
                return self._do_event(it)
            return self._do_memo(it)
        except Exception as e:  # noqa: BLE001
            return self._fail(it.text, f"{_KIND_LABEL[it.kind]}写入失败：{e}",
                              kind=it.kind)

    # ── 三条写入路径

    def _do_todo(self, it: Item) -> Outcome:
        # 待办可以带时间（"明天交电费"），但**归属仍是提醒事项** ——
        # 时间不改变归属，只是"什么时候做"的提示。
        #
        # 刻意**不设到期日**：给每条待办都设 due 会制造假紧迫感
        # （到期弹通知、变红），而用户要的是"记下来别忘了"，不是催命。
        # 时间由提醒事项的备注携带（见 _real_add_todo）。
        due = it.when.start if (it.when and it.when.has_date) else None
        ref = self.add_todo(it.text, due)
        self._journal("todo_added", text=it.text, reminder_id=ref, ok=True)
        return Outcome(ok=True, kind=Kind.TODO, text=it.text,
                       reply=_reply_ok(it, ref), ref_id=ref)

    def _do_event(self, it: Item) -> Outcome:
        """
        写日程。时间与标题都已在 `routes.route()` 里定好 ——
        这里不再做任何判断（包括"已过时刻顺延"，那条也在路由层）。
        """
        import whens

        # `it.when` 不可能是 None：routes 保证日程必有时间，否则抛 RouteError。
        # 但仍显式拦一道，避免将来有人在 routes 里放松了约束而这里静默写空。
        if it.when is None:
            return self._fail(it.text, "日程没有时间（路由层应已拦下）",
                              kind=Kind.EVENT)

        alarm = _alarm_for(it)
        ref = self.add_event(it.text, it.when.start, it.when.end,
                             recurrence=it.recurrence, allday=it.when.all_day,
                             alarm=alarm)
        self._journal("event_added", summary=it.text,
                      start=it.when.start.isoformat(),
                      end=it.when.end.isoformat(),
                      calendar="", ok=True, recurrence=it.recurrence,
                      alarm=alarm,
                      **({"rolled_to_next_day": True} if it.rolled else {}))

        reply = _reply_ok(it, ref)
        if it.rolled:
            reply += ("\n　　（这个时间今天已经过了，我放到了明天 —— "
                      "要改就去日历里改）")
        return Outcome(ok=True, kind=Kind.EVENT, text=it.text,
                       reply=reply, ref_id=ref,
                       when_text=whens.format_when(it.when))

    def _do_memo(self, it: Item) -> Outcome:
        ref = self.add_memo(it.text)
        self._journal("memo_added", text=it.text, memo_id=ref, ok=True)
        return Outcome(ok=True, kind=Kind.MEMO, text=it.text,
                       reply=_reply_ok(it, ref), ref_id=ref)

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

    ## 备注里**不再写去重键**（2026-10-05 改，用户提出）

    原先备注是 `pdca:<内容指纹> · 10-05`。那个 `pdca:xxxx` 是 **v1 的产物**：
    v1 靠它把"备忘录当天页里的行"与"提醒事项里的条目"对上
    （见 `reminders.make_key` 的注释：指纹 + 日期，同一天内幂等）。

    v4 不需要它，理由不止一条：

      · v4 **只增不去重**（"一条消息 = 一条新记录"是明确行为），幂等没有用武之地；
      · 靠它匹配的那条链路（`completion` / `push_tasks` / `carry_over`）整体下线，
        launchd 里只剩 daemon 与 report 两个任务（其余在 `deploy/legacy/`）；
      · v4 真要认"这条是我建的"时，用的是 journal 里的 `x-apple-reminder://` id ——
        比内容哈希可靠（你改一个字，哈希就变了）。

    所以它只是在你的列表里当噪音。备注现在只留时间提示；没给时间就空着。
    """
    import reminders
    rem = reminders.Reminders()
    rem.verify_list()
    body = ""
    if when is not None:
        # 只有日期时（时刻被归零）**不要**写 "00:00" ——
        # 那看起来像"半夜有安排"。全天/只给日期 → 只写日期。
        body = (when.strftime("%m-%d") if when.time() == dt.time(0, 0)
                else when.strftime("%m-%d %H:%M"))
    r = rem.create(name=text, body=body)
    return getattr(r, "id", "") or ""


def _real_add_event(summary: str, start: dt.datetime, end: dt.datetime,
                    location: str = "", recurrence: str = "",
                    allday: bool = False, alarm: bool = False) -> str:
    import applecal
    ev = applecal.add(summary, start, end, location=location,
                      recurrence=recurrence, allday=allday, alarm=alarm)
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

        def fake_event(summary, start, end, location="", recurrence="",
                       allday=False, alarm=False):
            written.append(f"event({summary!r}, {start} → {end}, "
                           f"rrule={recurrence!r}"
                           f"{' + 闹钟' if alarm else ''})")
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
