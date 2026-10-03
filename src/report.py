#!/usr/bin/env python3
"""
日报：读三处 Apple 应用的**当前状态**，生成复盘并推送。

## 三处数据源（全部只读）

    提醒事项 → 今天完成了什么、还剩什么
    日历     → 明天的日程
    备忘录   → 放久了还没处理的备忘（防遗忘）

**对 Apple 数据只读** —— 日报不改你三个 App 里的任何东西。这与 v1 不同：
v1 的日报会回写留档、还会同步提醒事项，于是"读"和"写"混在一起，
出了问题很难判断是谁改的。

> ⚠️ 一处**从 v1 命名继承下来的模糊说法**，在这里说清：
> 日报会往 `data/journal/` 追加"我观察到什么"（例如 `memo_cleared`），
> 所以严格讲它并非对**所有**东西只读。
> 但那不违反约束 —— journal 是**传感器读数**，不是状态源；
> 写它属于"记录观察"，不是"修改事实"。**Apple 应用仍然只读。**

## 数据源可注入

    build_report(date_str, reminder_items=..., events=..., memos=...)

生产用真实适配器，测试注入假数据 —— 于是全部渲染逻辑都能离线验证。

## 「防遗忘」怎么实现

从 `journal` 的台账（我提交过哪些备忘）里，找出**创建超过 N 天**的条目。
"某条还在不在"由 journal 台账回答（快照观察已把被删的移出台账），
所以日报**不需要读备忘录正文** —— 少一次 I/O，也少一处可能的失败。
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent

# 备忘放多久还没被删掉，就在日报里提醒一次（天）
MEMO_NAG_DAYS = 3


@dataclass
class Todo:
    """提自提醒事项的一条待办。"""
    name: str
    completed: bool
    completed_at: dt.datetime | None = None


@dataclass
class Event:
    """提自日历的一条日程。"""
    summary: str
    start: dt.datetime
    location: str = ""


@dataclass
class Memo:
    """提自 journal 台账的一条备忘。"""
    text: str
    created: dt.datetime | None = None


@dataclass
class ReportData:
    """日报的全部输入（三处快照）。"""
    date: dt.date
    todos: list[Todo] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)      # 明天的日程
    memos: list[Memo] = field(default_factory=list)        # 需要提醒的备忘
    errors: list[str] = field(default_factory=list)        # 某处读失败时的说明

    @property
    def done(self) -> list[Todo]:
        return [t for t in self.todos if t.completed]

    @property
    def open_items(self) -> list[Todo]:
        return [t for t in self.todos if not t.completed]


# ── 渲染

def build_report(data: ReportData) -> tuple[str, str]:
    """
    生成 (标题, 正文)。

    正文结构固定四段，**每段都可能为空**（空则整段省略）：
        完成 / 未完成 / 明日日程 / 备忘提醒
    """
    d = data.date
    title = f"📋 {d.isoformat()} 复盘"

    lines: list[str] = []

    if data.done:
        lines.append(f"✅ 今日完成 {len(data.done)} 件")
        for t in data.done:
            lines.append(f"　{t.name}")
        lines.append("")

    if data.open_items:
        lines.append(f"⏳ 未完成 {len(data.open_items)} 件")
        for t in data.open_items:
            lines.append(f"　{t.name}")
        lines.append("")
    elif data.todos:
        lines.append("🎉 今天的待办全部完成了")
        lines.append("")

    # 注意：读取失败时**不能**说"没有待办" —— 那是把"读不到"说成"没有"，
    # 会误导（实测见过：权限缺失时日报显示"今天没有待办"，看起来一切正常）。
    _rem_failed = any("提醒事项" in e for e in data.errors)
    if not data.todos and not _rem_failed:
        lines.append("（提醒事项里还没有条目 —— 发一句给我就行）")
        lines.append("")

    if data.events:
        lines.append(f"📅 明日日程 {len(data.events)} 项")
        for e in data.events:
            loc = f"　@{e.location}" if e.location else ""
            lines.append(f"　{e.start.hour:02d}:{e.start.minute:02d} "
                         f"{e.summary}{loc}")
        lines.append("")

    if data.memos:
        lines.append(f"📝 备忘放了 {MEMO_NAG_DAYS} 天以上，还没处理：")
        for m in data.memos:
            lines.append(f"　{m.text}")
        # 这句话现在**是真的**：上面 _read_stale_memos 做了只读快照差集，
        # 你在备忘录里删掉的条目会被记成 memo_cleared 并从此不再出现在这里。
        # （曾经这里写过同样的话，但差集没接上 —— 见该函数的注释。）
        lines.append("　（处理完在备忘录里删掉即可，之后不再提醒）")
        lines.append("")

    if data.errors:
        lines.append("⚠️ 以下来源读取失败，本份可能不完整：")
        for e in data.errors:
            lines.append(f"　{e}")
        lines.append("")

    lines.append("─" * 30)
    lines.append("做完的在「提醒事项」里打钩 ✓")
    lines.append("（发一句给我也行，比如「明天交电费」）")

    return title, "\n".join(lines).strip()


# ── 数据采集（真实路径，全部只读）

def collect(date: dt.date | None = None,
            memo_nag_days: int = MEMO_NAG_DAYS) -> ReportData:
    """
    读三处 Apple 应用的当前状态。

    **每处失败都不影响其它处** —— 日报的价值在于汇总，
    宁可少一段也不要整份失败（v1 的教训：一个来源不可用就整个日报发不出）。
    """
    today = date or dt.date.today()
    data = ReportData(date=today)

    # ① 提醒事项（"今日完成"按原生 completion date 筛）
    try:
        data.todos = _read_todos(today)
    except Exception as e:  # noqa: BLE001
        data.errors.append(f"提醒事项：{e}")

    # ② 日历（明天的日程）
    try:
        data.events = _read_events(today + dt.timedelta(days=1))
    except Exception as e:  # noqa: BLE001
        data.errors.append(f"日历：{e}")

    # ③ 备忘（从 journal 台账算，不读备忘录正文）
    try:
        data.memos = _read_stale_memos(today, memo_nag_days)
    except Exception as e:  # noqa: BLE001
        data.errors.append(f"备忘台账：{e}")

    return data


def _read_todos(day: dt.date | None = None) -> list[Todo]:
    """
    读提醒事项列表里的条目。

    ⚠️ 「今日完成」**必须按完成日期筛**，否则昨天、上个月完成的条目
    会永远堆在"今日完成"里，日报越看越不可信。
    判据用 Apple 原生的 `completion date`
    （探测见 tools/probe-native-dates.py：已完成条目可读，
      未完成条目是 missing value）。

    `day` 为 None 时（兼容旧调用）不筛完成项 —— 但**新调用都应该传日期**。
    """
    import reminders
    rem = reminders.Reminders()
    rem.verify_list()
    out: list[Todo] = []
    for r in rem.all_reminders():
        # 未完成项一律保留（它们没有"哪天完成的"这个问题）
        if r.completed and day is not None:
            # 没有完成时刻的已完成项：宁可漏报一条，也不要把它算进"今天完成"
            if r.completed_at is None or r.completed_at.date() != day:
                continue
        out.append(Todo(
            name=r.name,
            completed=bool(r.completed),
            completed_at=r.completed_at,
        ))
    return out


def _read_events(day: dt.date) -> list[Event]:
    """读某一天的日程。"""
    import applecal
    evs = applecal.events_between(day, day + dt.timedelta(days=1))
    return [Event(summary=e.summary, start=e.start, location=e.location)
            for e in evs]


def _read_stale_memos(today: dt.date, days: int) -> list[Memo]:
    """
    从 journal 台账里找"放了 N 天以上"的备忘。

    ⚠️ 台账（journal.submitted_memos）**只回答"我提交过什么"**，
    不回答"它现在还在不在"——后者是 Apple 应用的事实。所以这里必须
    再做一次**只读快照差集**：

        台账里有、快照里没有  → 你已经删了 → 记 memo_cleared，永不再提醒
        台账里有、快照里也有  → 还没处理   → 够天数就提醒

    这就是 ARCHITECTURE §四写的"感知路径"。

    **曾经这里只读台账、没做差集**，于是你删掉备忘、日报照样天天提醒它，
    而页脚还写着"删掉即可，之后不再提醒"——**承诺了没实现的功能**。
    探测（tools/probe-native-dates.py）证实备忘录**没有**"这条被删了"的
    原生线索（删除只是移进 Recently Deleted 保留 30 天），
    所以判断"还在不在"只能靠快照差集，不能靠时间戳。

    差集是纯读 + 记一笔日志，不改任何 Apple 数据。
    """
    import journal

    ledger = journal.submitted_memos(days=365)

    # 只读快照：拿"现在真实还在的 id 集合"。
    # 快照失败时**不做差集**（读不到 ≠ 被删了）—— 这一条很关键：
    # 把"没读到"当成"被删除"会静默地把提醒全部清掉，而那看不出来。
    live_ids: set[str] | None = None
    try:
        import memo
        live_ids = memo.snapshot_ids()
    except Exception as e:  # noqa: BLE001
        # 读不到就退化成"只按台账提醒"（宁可多提醒，也不要误判为已删除）
        import sys as _sys
        print(f"（备忘快照读取失败，本次不做差集：{e}）", file=_sys.stderr)

    out: list[Memo] = []
    for mid, info in ledger.items():
        if live_ids is not None and mid not in live_ids:
            # 你已删除 → 记一笔，之后永不再提
            try:
                journal.log_memo_cleared(mid, info.get("text", ""))
            except Exception:  # noqa: BLE001
                pass          # 记不上不影响本份报告
            continue
        created = _parse_at(info.get("at", ""))
        if created is None:
            continue
        age = (today - created.date()).days
        if age >= days:
            out.append(Memo(text=info.get("text", ""), created=created))
    out.sort(key=lambda m: m.created or dt.datetime.min)
    return out


def _parse_at(s: str) -> dt.datetime | None:
    """解析 journal 里的 ISO 时间戳。容错：解析不了返回 None。"""
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            return dt.datetime.fromisoformat(s[:19])
        except ValueError:
            return None


# ── 主流程

def run(date: dt.date | None = None, push: bool = True,
        channels: list[str] | None = None,
        memo_nag_days: int = MEMO_NAG_DAYS,
        data: ReportData | None = None,
        sender: Callable[..., list] | None = None) -> tuple[str, str, bool]:
    """
    生成并（可选）推送日报。返回 (标题, 正文, 推送是否至少一个通道成功)。

    `data` 给定时跳过采集（测试用）；`sender` 给定时跳过真实推送。

    ⚠️ 第三个返回值是给**退出码**用的。原先 run 不返回推送结果，
    main 于是永远返回 0 —— 推送全失败时 launchd 仍显示"成功"，
    你会以为日报发出去了。**静默失败比报错更危险**，所以必须如实上报。
    """
    today = date or dt.date.today()
    data = data or collect(today, memo_nag_days)
    title, body = build_report(data)

    print(title)
    print("═" * 46)
    print(body)
    print("═" * 46)

    # 存档到 data/digest/（Telegram 之外再留一份，便于回顾）
    try:
        digest_dir = ROOT / "data" / "digest"
        digest_dir.mkdir(parents=True, exist_ok=True)
        (digest_dir / f"{today.isoformat()}.md").write_text(
            f"# {title}\n\n{body}\n", encoding="utf-8")
        print(f"已存档：data/digest/{today.isoformat()}.md")
    except OSError as e:
        print(f"⚠️ 存档失败（不影响推送）：{e}", file=sys.stderr)

    pushed_ok = True          # 未推送（--no-push）视为成功
    if push:
        import notify
        send = sender or notify.broadcast
        results = send(title, body, channels=channels)
        print()
        for ch, ok, msg in results:
            print(f"  {'✅' if ok else '❌'} {ch}: {msg}")
        # 所有通道都失败才算失败 —— 两个通道互为冗余，一个成功就够了
        pushed_ok = any(ok for _, ok, _ in results)
        if not pushed_ok:
            print("⚠️ 所有通道都推送失败（日报已存档，但没送到你手上）",
                  file=sys.stderr)

    return title, body, pushed_ok


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="日报：汇总三处状态并推送")
    ap.add_argument("date", nargs="?", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--no-push", action="store_true", help="只打印，不推送")
    ap.add_argument("--channels", default="telegram,bark",
                    help="推送通道，逗号分隔（默认 telegram,bark）")
    ap.add_argument("--memo-days", type=int, default=MEMO_NAG_DAYS,
                    help=f"备忘放多少天开始提醒（默认 {MEMO_NAG_DAYS}）")
    args = ap.parse_args()

    try:
        d = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    except ValueError as e:
        print(f"❌ 日期格式不对（应为 YYYY-MM-DD）：{e}", file=sys.stderr)
        return 1

    _, _, pushed = run(
        d, push=not args.no_push,
        channels=[c.strip() for c in args.channels.split(",") if c.strip()],
        memo_nag_days=args.memo_days)
    # 退出码要如实反映结果：launchd 靠它标记成功/失败。
    # 全通道推送失败 → 非 0，这样 `launchctl print` 里能看到失败，
    # 而不是显示"成功"让你以为报表发出去了。
    return 0 if pushed else 3


if __name__ == "__main__":
    sys.exit(main())
