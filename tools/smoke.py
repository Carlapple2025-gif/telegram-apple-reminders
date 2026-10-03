#!/usr/bin/env python3
"""
端到端冒烟测试：模拟一批 Telegram 消息走完整条链路。

## 为什么需要它

各模块都有单元测试，但**"一条消息进来后到底落到哪、日志写了什么"**
这条链路没有整体验证过。而这类"接线"问题恰恰是本项目反复踩的坑
（write_back 没被调用、顺延后没建留档、日报里的待办从没进过提醒事项）。

这个测试把三个写入端换成假的，于是：

    Telegram 消息 → 分类 → 分派 → journal
                              （真实 journal，写到临时目录）

全程不碰 Apple 应用、不需要授权、不联网。

## 跑法

    python3 tools/smoke.py          # 正常跑
    python3 tools/smoke.py --keep   # 保留临时目录（排查用）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import journal          # noqa: E402
import intake           # noqa: E402
import report           # noqa: E402
import daemon           # noqa: E402
import datetime as dt   # noqa: E402

# 模拟的一批消息：覆盖三条分派路径 + 两种"需要确认"的情况
MESSAGES = [
    ("明天交电费", "todo"),
    ("勘察表盖章", "todo"),
    ("周五下午两点项目周会", "event"),
    ("下周三体检", "event"),
    ("每周一交周报", "event"),
    ("想起一件事，荷载要按名称命名", "memo"),
    ("帮我看下那个表", "ask"),        # 判不出 → 不写入，问一次
    ("例会", "ask"),                  # 日程缺时间 → 不写入，要求补充
]


class Recorder:
    """假的三个写入端：记录调用而不是真写。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self._n = 0

    def _next(self, kind: str) -> str:
        self._n += 1
        return f"{kind.upper()}-{self._n}"

    def todo(self, text, when=None):
        self.calls.append(("todo", text, when))
        return self._next("todo")

    def event(self, summary, start, end, location="", recurrence="", allday=False):
        self.calls.append(("event", summary, start, recurrence, allday))
        return self._next("event")

    def memo(self, text):
        self.calls.append(("memo", text))
        return self._next("memo")


def main() -> int:
    keep = "--keep" in sys.argv

    tmp = Path(tempfile.mkdtemp(prefix="pdca-smoke-"))
    # journal 与 daemon 状态都指向临时目录（不污染真实数据）
    journal.JOURNAL_DIR = tmp / "journal"
    daemon.STATE_FILE = tmp / "state.json"
    daemon.PENDING_DIR = tmp / "pending"

    failures: list[str] = []
    checks = 0

    def check(label: str, cond: bool, extra: str = "") -> None:
        nonlocal checks
        checks += 1
        if not cond:
            failures.append(f"{label} {extra}")

    base = dt.date(2026, 10, 3)          # 周六，固定基准日
    rec = Recorder()
    it = intake.Intake(add_todo=rec.todo, add_event=rec.event, add_memo=rec.memo)

    print("═" * 58)
    print("端到端冒烟：模拟 Telegram 消息走完整条链路")
    print(f"临时目录：{tmp}")
    print("═" * 58)
    print()

    results: list[tuple[str, str, str]] = []
    for i, (text, expect) in enumerate(MESSAGES, 1):
        before = len(rec.calls)
        out = it.handle(text, base, msg_id=1000 + i)
        wrote = rec.calls[before:]
        got = "ask" if out.needs_ask else (out.kind.value if out.kind else "?")
        mark = "✅" if got == expect else "❌"
        if got != expect:
            failures.append(f"「{text}」期望 {expect}，得到 {got}")
        checks += 1

        detail = ""
        if wrote:
            w = wrote[0]
            if w[0] == "event":
                detail = f"→ 日历 {w[2]} [{w[3] or '不重复'}]"
            else:
                detail = f"→ {w[0]}"
        else:
            detail = "→ 未写入（要求确认）"
        results.append((text, got, detail))
        print(f"  {mark} {text!r:<32} {got:<6} {detail}")

    print()
    print("─" * 58)
    print("核对落点与台账")
    print("─" * 58)

    kinds = [c[0] for c in rec.calls]
    check("写入次数与预期一致（8 条里 6 条写入、2 条提问）",
          len(rec.calls) == 6, f"实际 {len(rec.calls)}")
    check("待办走提醒事项", kinds.count("todo") == 2, str(kinds))
    check("日程走日历", kinds.count("event") == 3, str(kinds))
    check("备忘走备忘录", kinds.count("memo") == 1, str(kinds))

    # 时间算得对不对（这是最容易错的地方）
    ev = [c for c in rec.calls if c[0] == "event"]
    weekly = [c for c in ev if c[3]]
    check("周期性日程带 RRULE", len(weekly) == 1 and "BYDAY=MO" in weekly[0][3],
          str([c[3] for c in ev]))
    timed = [c for c in ev if not c[3]]
    check("定时日程的时间正确",
          any(c[2] == dt.datetime(2026, 10, 9, 14, 0) for c in timed),
          str([c[2] for c in timed]))

    # journal 必须真的记下来（这里守的是"静默写不进去"那个 bug）
    recs = journal.read_day(journal._today())
    evs = [r["event"] for r in recs]
    check("journal 记了 8 条 input（每条消息都留痕）",
          evs.count("input") == 8, str(evs.count("input")))
    check("journal 记了 todo_added", evs.count("todo_added") == 2, str(evs))
    check("journal 记了 event_added", evs.count("event_added") == 3, str(evs))
    check("journal 记了 memo_added", evs.count("memo_added") == 1, str(evs))

    # 台账：我提交过哪些备忘（供日报的"防遗忘"用）
    subs = journal.submitted_memos()
    check("备忘台账有 1 条", len(subs) == 1, str(list(subs)))

    print(f"  journal 记录 {len(recs)} 条，事件分布："
          f"{ {e: evs.count(e) for e in sorted(set(evs))} }")
    print(f"  备忘台账：{list(subs.values())[0]['text'] if subs else '（空）'}")
    print()

    # 日报：用同一个 journal 台账渲染，验证"防遗忘"能取到刚提交的备忘
    print("─" * 58)
    print("日报渲染（用刚产生的台账）")
    print("─" * 58)
    data = report.ReportData(
        date=base,
        todos=[report.Todo("交电费", False), report.Todo("勘察表盖章", True)],
        events=[report.Event("项目周会", dt.datetime(2026, 10, 4, 14, 0), "会议室")],
        memos=[report.Memo(v["text"], dt.datetime.fromisoformat(v["at"]))
               for v in subs.values()],
    )
    title, body = report.build_report(data)
    print(f"  {title}")
    for line in body.splitlines():
        print(f"  {line}")
    print()
    check("日报列出完成项", "勘察表盖章" in body)
    check("日报列出未完成项", "交电费" in body)
    check("日报列出明日日程", "项目周会" in body)
    check("日报含备忘（防遗忘）", "荷载要按名称命名" in body)

    # daemon 的状态持久化（重启不丢读取位置）
    print("─" * 58)
    print("守护状态持久化")
    print("─" * 58)
    daemon.save_offset(4321)
    check("offset 落盘可读回", daemon.load_offset() == 4321)
    daemon.save_pending(77, "帮我看下那个表")
    check("待确认项落盘可读回", daemon.load_pending(77) == "帮我看下那个表")
    daemon.clear_pending(77)
    check("待确认项可清除", daemon.load_pending(77) is None)
    print(f"  offset = {daemon.load_offset()}，待确认项已写入/读取/清除 ✓")
    print()

    print("═" * 58)
    if failures:
        print(f"❌ 失败 {len(failures)}/{checks} 项：")
        for f in failures:
            print(f"   · {f}")
    else:
        print(f"✅ 端到端全部通过（{checks} 项）")
    print("═" * 58)

    if keep:
        print(f"临时目录保留：{tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
