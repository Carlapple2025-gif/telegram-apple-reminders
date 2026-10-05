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

import whens
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent

# 备忘放多久还没被删掉，就在日报里提醒一次（天）
MEMO_NAG_DAYS = 3

# 日报的计划时间。**必须与 deploy/com.carl.pdca.report.plist 的
# StartCalendarInterval 保持一致**（自检里有一条断言盯着这个一致性）。
#
# 为什么要在这里也知道计划时间：launchd 的语义是"睡过了就在唤醒时补跑"
# （man launchd.plist：coalesced into one event upon wake），所以 21:30 的
# 日报完全可能在第二天早上才发出 —— 而正文里若没有生成时刻，你**看不出来**。
SCHEDULE_HOUR = 21
SCHEDULE_MINUTE = 30

# 比计划时间前后差多少分钟算"不在计划时间上"（要在日报里明确说出来）。
# 两边都要管：
#   · 晚 —— 机器在 21:30 没醒，唤醒后补跑（22:15、23:50 都算）
#   · 早 —— 睡过了 21:30，凌晨才醒，于是**次日 00:40** 跑了一份"新一天"的日报：
#          它数据没错，但内容是空的，而且昨天那份**永远不会来了**。
#          不标出来的话，你只会看到一份莫名其妙的空日报。
SCHEDULE_GRACE_MIN = 30

# 某条通道连续多少天没成功，就在日报里提示（否则单通道静默失效能瞒你几个月）
CHANNEL_WARN_DAYS = 3


# ── 数据来源的名字（**精确匹配用，不做子串匹配**）
#
# ⚠️ 这几个常量存在的理由是一条踩过的坑：判断"提醒事项这次读到了吗"曾经写成
# `any("提醒事项" in e for e in data.errors)` —— 拿**字符串包含**去反查来源。
# 那在"来源名恰好是另一个来源名的子串"、或文案一改时会**静默失效**，
# 而它守的正是"读不到不能说成没有"这条最不能失效的约定。
#
# 现在 errors 是结构化的（`SourceError`），判断用 `data.failed(SRC_REMINDERS)`：
# 来源必须**相等**，不是包含。
SRC_REMINDERS = "提醒事项"
SRC_CALENDAR = "日历"
SRC_MEMOS = "备忘台账"
SRC_CHANNELS = "通道读数"


@dataclass(frozen=True)
class SourceError:
    """某一处数据源这次读取失败了（**读不到 ≠ 没有**的载体）。"""
    source: str          # 见上面四个常量
    detail: str          # 原始异常文本，如实保留

    def __str__(self) -> str:
        # 渲染成"来源：原因"，与 errors 还是字符串时的输出**逐字一致**
        return f"{self.source}：{self.detail}"


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
    end: dt.datetime | None = None      # /list 要用它判"今天这场还没结束"
    all_day: bool = False               # 全天日程不能显示成 00:00
    recurrence: str = ""                # RRULE；非空 = 重复事件的某一次发生


@dataclass
class Memo:
    """提自 journal 台账的一条备忘。"""
    text: str
    created: dt.datetime | None = None


@dataclass
class ReportData:
    """日报的全部输入（三处快照 + 投递读数）。"""
    date: dt.date
    todos: list[Todo] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)      # 明天的日程
    memos: list[Memo] = field(default_factory=list)        # 需要提醒的备忘
    errors: list[SourceError] = field(default_factory=list)  # 哪几处读失败了
    # 生成本份的时刻 —— 也就是**数据截止时刻**。为空时正文不写这一行
    # （测试里注入假数据时可以不管它）。
    generated_at: dt.datetime | None = None
    # 本次运行相对计划时间（21:30）的偏移分钟数：正=晚、负=早、None=不适用
    # （比如补跑历史日期）。见 SCHEDULE_GRACE_MIN 的说明。
    schedule_offset: int | None = None
    # 投递通道的读数提示（如"telegram 已连续 3 天投递失败"）
    channel_warnings: list[str] = field(default_factory=list)
    # "上一份日报是什么时候"的提示（连续漏跑后在恢复时说出来）
    digest_gap_note: str = ""
    # 这次实际用的"备忘放几天开始提醒"。
    #
    # ⚠️ 正文里那句「📝 备忘放了 N 天以上」必须用**这次实际用的** N，
    # 不能用模块常量：`--memo-days 7` 跑出来的日报若还写"3 天以上"，
    # 那就是正文在撒谎（本机实测过这个不一致：筛的是 7 天，写的是 3 天）。
    memo_nag_days: int = MEMO_NAG_DAYS

    @property
    def done(self) -> list[Todo]:
        return [t for t in self.todos if t.completed]

    @property
    def open_items(self) -> list[Todo]:
        return [t for t in self.todos if not t.completed]

    def failed(self, source: str) -> bool:
        """这一处数据源这次读取失败了吗？**按精确来源判，不做子串匹配。**"""
        return any(e.source == source for e in self.errors)


# ── 渲染：区块注册表
#
# 日报正文由一串**区块**组成，每个区块自己回答"这次要不要出现"。
#
# 为什么改成注册表（2026-10-05）：原先正文是 `build_report()` 里一列顺序 if，
# 于是"加一段"= 改渲染主函数 —— 而主函数是**每份产物都要经过**的公共路径。
#
# ⚠️ 这不是为了优雅，是为了**下一个功能**：README §八 的 P1 是周报，
# 而周报 = 同一份数据、不同的区块选择。没有这张表时，加周报要复制一遍
# `build_report`（"同一件事两处实现"，本项目反复踩过的坑）。
#
# 区块的两条硬约定：
#   ① `render` 返回若干行；返回**空列表 = 整段省略**。
#      判断"该不该出现"是区块自己的事，`build_report` 不替任何区块判断。
#   ② 区块只许读 `ReportData`，不许读模块常量来做判断
#      —— `memo_nag_days` 就是因为这条从常量搬进了 data（见 ReportData 的注释）。

@dataclass(frozen=True)
class Block:
    """日报正文的一个区块。"""
    id: str                                     # 稳定标识：断言、摘要、文档都用它
    render: Callable[[ReportData], list[str]]   # 若干行；空列表 = 省略
    # 心跳摘要里的短名。**非空 = 会出现在对外的计数摘要里**（第三方存储，
    # 见 _heartbeat_summary）。这是个隐私决定，所以写在区块自己身上。
    heartbeat: str = ""
    # 这一块在心跳摘要里报的**数字**（只有配了 heartbeat 才用得上）。
    # 单列一个函数而不是从 render 数行数：render 的行数 ≠ 条目数
    # （标题行、页脚行、"删掉即可"那种说明行都会算进去）。
    count: Callable[[ReportData], int] | None = None
    # 这一块出现在哪些产物里。"daily" = 21:30 的日报；周报将来用别的名字。
    digests: tuple[str, ...] = ("daily",)
    note: str = ""                              # 为什么这样写（给人看）


def _schedule_flags(data: ReportData) -> tuple[bool, bool]:
    """(晚了, 早了)。偏离计划时间超过 SCHEDULE_GRACE_MIN 才算 —— 两个方向都要管。"""
    off = data.schedule_offset
    return (off is not None and off > SCHEDULE_GRACE_MIN,
            off is not None and off < -SCHEDULE_GRACE_MIN)


def _title(data: ReportData) -> str:
    late, early = _schedule_flags(data)
    mark = "（延迟）" if late else ("（非计划时间）" if early else "")
    return f"📋 {data.date.isoformat()} 复盘{mark}"


def _blk_generated_at(data: ReportData) -> list[str]:
    """🕘 生成时刻 = 数据截止时刻。

    写在最前面，因为"这份数据有多新"决定了后面每一行该不该信
    —— 详见 CONCEPT.md 的 P0「摘要必须带数据截止时间，陈旧就明确告警」。
    """
    if not data.generated_at:
        return []
    late, early = _schedule_flags(data)
    out = [f"🕘 生成于 {data.generated_at.strftime('%H:%M')}（数据截至同一时刻）"]
    _hhmm = f"{SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d}"
    if late:
        out.append(f"⚠️ 比计划（{_hhmm}）晚 {_fmt_offset(data.schedule_offset)}"
                   f" —— 多半是机器在计划时间没醒，唤醒后才补跑")
    elif early:
        out.append(f"⚠️ 比计划（{_hhmm}）早 {_fmt_offset(data.schedule_offset)}"
                   f" —— 本次**不是** {_hhmm} 那一趟（机器唤醒后补跑，"
                   f"或手工触发）；也就是说上一份日报没有发出")
    out.append("")
    return out


def _blk_done(data: ReportData) -> list[str]:
    if not data.done:
        return []
    out = [f"✅ 今日完成 {len(data.done)} 件"]
    out += [f"　{t.name}" for t in data.done]
    out.append("")
    return out


def _blk_open_items(data: ReportData) -> list[str]:
    """未完成段。空且**有**待办时改说「全部完成」——那是唯一该庆祝的时刻。"""
    if data.open_items:
        out = [f"⏳ 未完成 {len(data.open_items)} 件"]
        out += [f"　{t.name}" for t in data.open_items]
        out.append("")
        return out
    if data.todos:
        return ["🎉 今天的待办全部完成了", ""]
    return []


def _blk_reminders_empty(data: ReportData) -> list[str]:
    """提醒事项里一条都没有时的提示。

    ⚠️ 读取失败时**不能**说"没有待办" —— 那是把"读不到"说成"没有"，
    会误导（实测见过：权限缺失时日报显示"今天没有待办"，看起来一切正常）。
    判据是"没有条目 **且** 这一处没读失败"，而且来源用**精确匹配**
    （`failed()` 而不是字符串包含 —— 见 SRC_* 常量的注释）。
    """
    if data.todos or data.failed(SRC_REMINDERS):
        return []
    return ["（提醒事项里还没有条目 —— 发一句给我就行）", ""]


def _blk_events(data: ReportData) -> list[str]:
    if not data.events:
        return []
    out = [f"📅 明日日程 {len(data.events)} 项"]
    for e in data.events:
        loc = f"　@{e.location}" if e.location else ""
        when = ("全天" if e.all_day
                else f"{e.start.hour:02d}:{e.start.minute:02d}")
        # 重复事件的某一次发生要标出来，否则"明天的跑步"看起来像一次性安排
        rec = f"（{whens.rrule_text(e.recurrence)}）" if e.recurrence else ""
        out.append(f"　{when} {e.summary}{rec}{loc}")
    out.append("")
    return out


def _blk_memos(data: ReportData) -> list[str]:
    if not data.memos:
        return []
    out = [f"📝 备忘放了 {data.memo_nag_days} 天以上，还没处理："]
    for m in data.memos:
        # 备忘正文**可以是多行的**（2026-10-04 起路由层保留换行）。
        # 日报里只放首行：否则一条备忘就能把日报撑开好几行，段落结构也会被带乱。
        first, *rest = (m.text or "").splitlines() or [""]
        out.append(f"　{first}" + ("　…" if rest else ""))
    # 这句话是**真的**：_read_stale_memos 做了只读快照差集，
    # 你在备忘录里删掉的条目会被记成 memo_cleared 并从此不再出现在这里。
    # （曾经这里写过同样的话，但差集没接上 —— 见该函数的注释。）
    out.append("　（处理完在备忘录里删掉即可，之后不再提醒）")
    out.append("")
    return out


def _blk_errors(data: ReportData) -> list[str]:
    if not data.errors:
        return []
    out = ["⚠️ 以下来源读取失败，本份可能不完整："]
    out += [f"　{e}" for e in data.errors]
    out.append("")
    return out


def _blk_channel_warnings(data: ReportData) -> list[str]:
    """某条通道已经连续几天没成功了。

    注意区分：这**不是**"本次投递失败"（那会走心跳告警），
    而是"某条通道已经好几天没成功了" —— 单通道静默失效只有这里能看见。
    """
    if not data.channel_warnings:
        return []
    out = ["⚠️ 投递通道读数："]
    out += [f"　{w}" for w in data.channel_warnings]
    out.append("")
    return out


def _blk_digest_gap(data: ReportData) -> list[str]:
    """与看门狗分工：看门狗管"当天 23:30 还没送到"（实时），
    这一行管"漏了几天，现在补上了"（事后对账）。"""
    if not data.digest_gap_note:
        return []
    return [f"⚠️ {data.digest_gap_note}", ""]


# 正文区块：**顺序就是渲染顺序**。
# 加一段正文 = 写一个 `_blk_*` 函数 + 在这里加一行（不必动 build_report）。
BLOCKS: tuple[Block, ...] = (
    Block("generated_at", _blk_generated_at,
          note="数据截止时刻 + 迟到/早到说明"),
    Block("done", _blk_done, heartbeat="完成",
          count=lambda d: len(d.done),
          note="按 Apple 原生完成时刻筛过"),
    Block("open_items", _blk_open_items, heartbeat="未完成",
          count=lambda d: len(d.open_items),
          note="空且有待办时改说「全部完成」"),
    Block("reminders_empty", _blk_reminders_empty,
          note="读失败时**不许**说'没有待办'"),
    Block("events", _blk_events, heartbeat="明日日程",
          count=lambda d: len(d.events)),
    Block("memos", _blk_memos, heartbeat="备忘",
          count=lambda d: len(d.memos),
          note="来自 journal 台账的差集；天数用 data.memo_nag_days"),
    Block("errors", _blk_errors, note="读不到 ≠ 没有"),
    Block("channel_warnings", _blk_channel_warnings),
    Block("digest_gap", _blk_digest_gap),
)

# 页脚：任何一份产物都带着它，所以它不属于任何区块。
#
# 第二行**不能**写成"发一句给我也行" —— 它紧跟"打钩 ✓"，
# 读起来像"发一句就能打钩"，而机器人没有打钩能力：发一句只会**新建**一条。
# 所以写成它真正能做的事：加一条。顺带把符号表每天念一遍
# （SYMBOL-SCHEME §6 说好的缓解措施之一）。
FOOTER: tuple[str, ...] = (
    "─" * 30,
    "做完的在「提醒事项」里打钩 ✓",
    "想加一条就直接发：交电费 · # 想法 · @周五两点 周会",
)


def blocks_for(digest: str = "daily") -> list[Block]:
    """
    某份产物要渲染哪些区块（按声明顺序）。

    ⚠️ 一个区块都没有时**抛异常**，不返回空列表：产物名的笔误会走成
    "一份什么都没有的日报"，而那种失败是**静默**的 ——
    与"宁可报错，不可静默"同源（与 routes 的拒绝路径同一取舍）。
    """
    out = [b for b in BLOCKS if digest in b.digests]
    if not out:
        raise ValueError(f"没有为 {digest!r} 登记任何区块（产物会是空的）")
    return out


def build_report(data: ReportData, digest: str = "daily") -> tuple[str, str]:
    """
    生成 (标题, 正文)。

    正文 = 该产物选中的区块依次渲染（空区块自己省略）+ 页脚。
    **加一段正文不用改这个函数** —— 改 `BLOCKS`（见上面那段说明）。
    """
    lines: list[str] = []
    for b in blocks_for(digest):
        lines.extend(b.render(data))
    lines.extend(FOOTER)
    return _title(data), "\n".join(lines).strip()


# ── 数据采集（真实路径，全部只读）

def _fmt_offset(minutes: int | None) -> str:
    """把"差了多少分钟"写成人的说法（取绝对值）。"""
    m = abs(int(minutes or 0))
    if m < 60:
        return f"{m} 分钟"
    h, mm = divmod(m, 60)
    return f"{h} 小时{mm} 分" if mm else f"{h} 小时"


def schedule_offset_minutes(now: dt.datetime,
                            report_date: dt.date) -> int | None:
    """
    本次运行相对计划时间的偏移（分钟）：正=晚、负=早、None=不适用。

    只对"当天"的日报有意义：手工补跑历史日期（`report.py 2026-10-01`）
    不该被判成迟到或早到，所以日期不是今天就返回 None。
    """
    if report_date != now.date():
        return None
    planned = dt.datetime.combine(
        report_date, dt.time(SCHEDULE_HOUR, SCHEDULE_MINUTE))
    return int((now - planned).total_seconds() // 60)


# 通道失败说明里出现这些字样时，属于"没配"而不是"坏了"——
# 没配的通道不该天天在日报里被念（那是配置问题，不是故障）。
_NOT_CONFIGURED_HINTS = ("未配置", "没有可用的 Bark key")


def _channel_warnings(days: int = 14) -> list[str]:
    """
    从 journal 的 `digest_pushed` 读数里算出"哪条通道好久没成功了"。

    ⚠️ 这是**读数的汇总**，不是状态源 —— 与备忘录台账同一性质。
    """
    import journal

    out: list[str] = []
    for name, h in sorted(journal.channel_health(days).items()):
        n = int(h.get("consecutive_fail_days") or 0)
        if n < CHANNEL_WARN_DAYS:
            continue
        if any(s in str(h.get("last_detail", "")) for s in _NOT_CONFIGURED_HINTS):
            continue
        since = (h.get("last_ok") or "")[:16].replace("T", " ") or "从未成功"
        out.append(f"{name} 已连续 {n} 天投递失败（最近一次成功：{since}）")
    return out


def _digest_gap_note(today: dt.date, days: int = 30) -> str:
    """
    "上一份日报是什么时候" —— 连续漏跑后，在**恢复的这一份**里说出来。

    与看门狗的分工：看门狗管"当天 23:30 还没送到"（实时告警），
    这一行管事后对账（"中间漏了几次"）—— 机器整晚没醒时只有它能说话。

    没有历史读数时**不提示**：那既可能是"从没跑过"，也可能是"刚装的"，
    分不清就不猜（沿用 `_channel_warnings` 的同一条判据）。
    """
    import journal

    dates = [d for d in journal.digest_dates(days=days, end=today.isoformat())
             if d < today.isoformat()]
    if not dates:
        return ""
    last = max(dates)
    try:
        missed = (today - dt.date.fromisoformat(last)).days - 1
    except ValueError:
        return ""
    if missed <= 0:
        return ""
    return (f"上一份日报是 {missed + 1} 天前（{last}），"
            f"中间漏了 {missed} 次 —— 详情见 data/journal/ 的读数")


def collect(date: dt.date | None = None,
            memo_nag_days: int = MEMO_NAG_DAYS) -> ReportData:
    """
    读三处 Apple 应用的当前状态。

    **每处失败都不影响其它处** —— 日报的价值在于汇总，
    宁可少一段也不要整份失败（v1 的教训：一个来源不可用就整个日报发不出）。
    """
    today = date or dt.date.today()
    data = ReportData(date=today, memo_nag_days=memo_nag_days)

    # ① 提醒事项（"今日完成"按原生 completion date 筛）
    try:
        data.todos = _read_todos(today)
    except Exception as e:  # noqa: BLE001
        data.errors.append(SourceError(SRC_REMINDERS, str(e)))

    # ② 日历（明天的日程）
    try:
        data.events = _read_events(today + dt.timedelta(days=1))
    except Exception as e:  # noqa: BLE001
        data.errors.append(SourceError(SRC_CALENDAR, str(e)))

    # ③ 备忘（从 journal 台账算，不读备忘录正文）
    try:
        data.memos = _read_stale_memos(today, memo_nag_days)
    except Exception as e:  # noqa: BLE001
        data.errors.append(SourceError(SRC_MEMOS, str(e)))

    # ④ 投递读数（同样来自 journal：通道健康 + 上次投递是哪天）
    try:
        data.channel_warnings = _channel_warnings()
        data.digest_gap_note = _digest_gap_note(today)
    except Exception as e:  # noqa: BLE001
        data.errors.append(SourceError(SRC_CHANNELS, str(e)))

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


def is_all_day(start: dt.datetime, end: dt.datetime) -> bool:
    """
    这条日程是不是"全天"？

    日历**没有**把这个标志给我们（`applecal.Event` 里没有该字段），
    所以从存法反推：全天事件从当天 00:00 起、跨满 24 小时 ——
    我们自己写全天日程时就是这么存的（见 whens：`end = start + 1 天`）。
    判错的代价只是显示成 `00:00` 还是 `全天`，不会写错任何东西。

    （为什么值得判：`00:00 体检` 看起来像"半夜十二点的体检"。）
    """
    return (start.time() == dt.time(0, 0)
            and (end - start) >= dt.timedelta(days=1))


def _read_events(day: dt.date) -> list[Event]:
    """读某一天的日程。"""
    return read_events_between(day, day + dt.timedelta(days=1))


def read_events_between(start: dt.date, end: dt.date) -> list[Event]:
    """
    读一段日期内的日程（`end` 不含当天）。

    与 `read_events(day)` 走**同一份实现**（后者就是它的一日版）。
    为什么要这个入口：`/list` 要"今天 + 明天"，一次读取比读两次便宜 ——
    每次读取都包含一遍**重复事件扫描**，而那份扫描是全表的
    （AppleScript 没法按 recurrence 筛，见 applecal 的说明）。
    """
    import applecal
    evs = applecal.events_between(start, end)
    return [Event(summary=e.summary, start=e.start, location=e.location,
                  end=e.end, all_day=is_all_day(e.start, e.end),
                  recurrence=e.recurrence)
            for e in evs]


# ── 公开读取入口
#
# 日报（build_report）与 `/list` 命令**共用这两条实现**。
# 特意开成公开函数而不是让 commands.py 去调 `_read_todos`：
# 跨模块调私有函数，改动时没人知道还有第二个调用方 ——
# 本项目的"同一件事两处实现"就是这么长出来的。

def read_open_todos(day: dt.date | None = None) -> list[Todo]:
    """**未完成**的待办（已打钩的不出现在这里）。"""
    return [t for t in _read_todos(day) if not t.completed]


def read_events(day: dt.date) -> list[Event]:
    """某一天的日程。"""
    return read_events_between(day, day + dt.timedelta(days=1))


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
        sender: Callable[..., list] | None = None,
        heartbeat: Callable[..., tuple] | None = None,
        now: dt.datetime | None = None) -> tuple[str, str, bool]:
    """
    生成并（可选）推送日报。返回 (标题, 正文, 推送是否至少一个通道成功)。

    `data` 给定时跳过采集（测试用）；`sender` / `heartbeat` 给定时跳过真实推送
    与真实心跳（测试用）。`now` 给定时用它当"生成时刻"（测试"偏离计划"分支用）。

    ⚠️ 第三个返回值是给**退出码**用的。原先 run 不返回推送结果，
    main 于是永远返回 0 —— 推送全失败时 launchd 仍显示"成功"，
    你会以为日报发出去了。**静默失败比报错更危险**，所以必须如实上报。
    """
    generated_at = now or dt.datetime.now()
    today = date or generated_at.date()
    data = data or collect(today, memo_nag_days)
    data.date = today
    if data.generated_at is None:
        data.generated_at = generated_at
        data.schedule_offset = schedule_offset_minutes(generated_at, today)
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
    results: list[tuple[str, bool, str]] = []
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

        # 心跳：告诉**机器之外**的监控"这一份算完整并送到了"。
        # 判据刻意严格 —— 读失败也算失败（数据不完整同样需要你介入）。
        beat = heartbeat or getattr(notify, "send_heartbeat", None)
        if beat is not None:
            intact = not data.errors
            hb_ok, hb_msg = beat(
                pushed_ok and intact, summary=_heartbeat_summary(data, results))
            # "没配"是选择，不是故障：中性标记，不当告警（否则每晚刷一行警告，
            # 久了就没人看这一行了 —— 那正是"告警疲劳"）。
            hb_off = "未配置" in hb_msg
            print(f"  {'✅' if hb_ok else ('·' if hb_off else '⚠️')} 心跳: {hb_msg}")

        # 投递结果落进 journal（**不可再生的留痕**，而 logs/ 是可以随时清的）。
        # 失败不影响主流程，但要明说 —— 静默失败比报错危险。
        try:
            import journal
            journal.log_digest_pushed(
                results, digest_date=today.isoformat(),
                generated_at=generated_at.isoformat(timespec="seconds"),
                schedule_offset=data.schedule_offset,
                heartbeat=("未配置" if hb_off else hb_msg))
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 投递结果没能记进 journal（不影响送达）：{e}",
                  file=sys.stderr)

    return title, body, pushed_ok


def _heartbeat_summary(data: ReportData,
                       results: list[tuple[str, bool, str]]) -> str:
    """
    心跳请求体：**只放计数，不放内容**。

    心跳服务的日志是第三方存储，而日报正文里有你的待办原文 ——
    所以这里刻意只报数字（本机 logs/ 里引用你的内容都只留前 30 字符，
    对外发送更不该带原文）。

    ⚠️ 计数**从区块注册表取**（`Block.heartbeat` 非空者），不在这里另写一张表：
    原先这里硬编码了"完成/未完成/明日日程/备忘"四个计数，
    于是加一个品类要改**三处**（`ReportData`、`build_report`、这里），
    漏掉这里就表现为"日报上看得见、心跳摘要里没有"这种偏心的静默不一致。
    """
    ch = " ".join(f"{name}{'✓' if ok else '✗'}" for name, ok, _ in results)
    parts: list[str] = []
    for b in BLOCKS:
        if b.heartbeat and b.count is not None:
            parts.append(f"{b.heartbeat}{b.count(data)}")
    # "读取失败"不是区块（它没有正文段可省略），单独算
    parts.append(f"读取失败{len(data.errors)}")
    if data.schedule_offset is not None:
        parts.append(f"偏离计划{data.schedule_offset}分")
    if ch:
        parts.append(f"通道[{ch}]")
    return " ".join(parts)


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
