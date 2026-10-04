#!/usr/bin/env python3
"""
路由：把一条输入按**行首符号**决定归属。

这是 v4 收件链路里**唯一决定"写到哪个 App"的地方**，而它不做任何推断 ——
只做三件事：

    ① 认出行首符号（`parse.strip_leading_marker`）
    ② 决定类型（符号 → Kind；没有符号 → 待办）
    ③ 处理载荷（剥掉时间短语得到正文；日程另需解析时间）

## 符号表（见 docs/SYMBOL-SCHEME.md）

| 你写的 | 归属 | 备注 |
|---|---|---|
| `# 内容` | 备忘（备忘录） | |
| `@内容` | 日历 | 载荷里必须有可解析的时间，否则**报错不写入** |
| `- [ ] 内容` | 待办（提醒事项） | |
| 裸内容 | 待办 | **最高频情况，零符号** |

## 不做什么（都是刻意的不做）

| 不做 | 为什么 |
|---|---|
| 猜类型 | 类型由你声明。猜错会**静默写错 App**，而声明的错误是**看得见的** |
| 追问"这是哪一类" | 追问要跨消息记住东西 → 命中 `ARCHITECTURE.md` §十一 判据 1 |
| `@` 缺时间时退回当备忘 | 那是**替你改主意**。你说 `@` 就是要进日历 |
| `@` 缺时间时用默认时刻 | 最坏的一种：静默写入一条错时间的日程 |

`@` 缺时间的处理是**报错并拒绝写入** ——
与「宁可报错，不可静默」同源：报错是陈述事实，提问是要求你再交互一次。
"""

from __future__ import annotations

import datetime as dt
import re

import parse
import whens
from kinds import Item, Kind


class RouteError(ValueError):
    """路由/载荷有硬问题（例如日程没有可解析的时间）。"""


# ── 符号表
#
# ⚠️ 这三个符号的判定**在本模块**，不在 `parse.py`。
#
# 为什么：`parse.py` 的 `parse()` 是 **v1 一天一页**的解析器，
# 它的 kind 词表是 `todo|note|meta|unknown` —— **没有 `event`**。
# 把 `@` 判成 event 放进那边，v1 的 `parse()` 会拿到一个不认识的类型，
# `carry_over.build_plan` 于是把它当未知**静默丢掉**：
# 历史笔记里 `@中午 勘察表盖章` 从此不再被顺延。（实测踩到过。）
#
# 教训：**新符号的语义归新模块**，别让它污染 v1 的词表。
_MEMO_MARKERS = "#＃"
_EVENT_MARKERS = "@＠"
# v1 遗留的 `@时段` 前缀（`@中午` / `@下午` …）。
# 在新语义下 `@` 恒定是日历，所以这里**不做**兼容剥离 ——
# 但在 `parse.content_fingerprint()` 里做，那里是去重键的入口。
_LEGACY_SLOT = re.compile(r"^[@＠]\s*(?:上午|中午|下午|晚上|明天)\s*")

# 符号 → 类型。`None` 表示"没识别到符号"，落到默认类型。
_SYMBOL_KIND = {
    "todo": Kind.TODO,
    "note": Kind.MEMO,      # parse.py 沿用 v1 的词，v4 叫 memo
    "event": Kind.EVENT,
}

# 没有符号时的默认类型。
#
# ⚠️ 这是**用户明确选的**（2026-10-03）：裸输入默认给待办。
# 代价要记住：一段"感慨"如果不打 `#`，就会进提醒事项的打钩清单。
# 判错的代价是不对称的，而这个选择意味着接受"清单可能被塞东西"，
# 换来的是"最高频的动作零成本"。
DEFAULT_KIND = Kind.TODO


def route(text: str, base: dt.date | None = None) -> Item:
    """
    把一句输入变成 `Item`。**不写任何东西** —— 纯函数，可离线测。

    抛 `RouteError` 的唯一情况：载荷缺硬信息
    （日程没时间/只有时间、或只有符号没有内容）。

    调用方把它转成给用户的报错文案，**不写任何 App**。
    """
    raw = (text or "").strip()
    if not raw:
        raise RouteError("空消息")

    payload, kind = _split_symbol(raw)

    body = whens.clean_text(payload)
    if not body:
        raise RouteError("只有符号、没有内容")

    if kind is Kind.EVENT:
        return _route_event(body, raw, base)

    # 待办与备忘：正文里可能带时间提示（"明天交电费"）。
    # 时间**不改变归属**，只是"什么时候做"的提示 ——
    # 待办把它放进提醒事项的备注，备忘则留在正文里。
    when = None
    if kind is Kind.TODO:
        when = whens.parse_when(body, base)
        stripped = whens.strip_time_phrases(body)
        if stripped:
            body = whens.clean_text(stripped)
    return Item(kind=kind, text=body, raw=raw, when=when)


def _split_symbol(raw: str) -> tuple[str, Kind]:
    """
    把输入拆成 (载荷, 类型)。**这是唯一决定归属的地方。**

    顺序说明：`@` 的日历判定排在 v1 遗留的 `@时段` 之前 ——
    否则 `@明天上午九点开会` 会被旧前缀吃掉"明天"，**丢掉日期**。
    历史写法（`@中午 勘察表盖章`）因此按新语义归日历；
    它的去重键由 `parse.content_fingerprint()` 保稳定。
    """
    s = raw.strip()

    if s[0] in _MEMO_MARKERS:
        return s[1:].lstrip(), Kind.MEMO
    if s[0] in _EVENT_MARKERS:
        return s[1:].lstrip(), Kind.EVENT

    # 其余交给 v1 的机械剥除（列表符号、复选框），它认得 `-` / `*` / `•`
    payload, v1_kind = parse.strip_leading_marker(s)
    return payload, _SYMBOL_KIND.get(v1_kind or "", DEFAULT_KIND)


def _route_event(body: str, raw: str, base: dt.date | None) -> Item:
    """
    日程：时间**必须**能解析出来，且**必须有事由**，否则报错。

    步骤（顺序重要）：
      ① 只有时间、没有事由 → 拒绝（见下方"为什么这条必须在最前"）
      ② 先解析重复规则 —— "每周一"这类要变成 RRULE
      ③ 有重复规则时，起始日取**下一次发生**（日历事件必须有 start）
      ④ 否则按普通时间解析
      ⑤ 剥掉句首时间短语，得到"事情本身"作为标题
    """
    ref = base or dt.date.today()

    # ① ⚠️ 必须在最前：`@上午九点` 这种"只有时间"的载荷，
    # 若先解析时间、后剥短语，会得到标题「上午九点」——
    # 而**这正是 2026-10-03 那次真实故障的形态**
    # （日历里多出一条标题是时间的日程，原事由丢失）。
    # 用 `is_bare_time` 判"整句只是个时间表达式"，判据在 whens 里
    # （时间词汇表的归属地，不在别处复制一份正则）。
    if whens.is_bare_time(body):
        raise RouteError(
            "这条日程只有时间、没有写是什么事 —— 请把事由一起写上")

    recur = whens.parse_recurrence(body)
    if recur:
        when = whens.first_occurrence(recur, ref)
        # ⚠️ 载荷里若**明说了时刻**，必须用上它。
        #
        # `first_occurrence()` 走的是"周期表达通常不带具体时刻"的默认
        # （09:00 + 全天）。于是 `@每天八点 跑步` 的"八点"会被**静默丢掉** ——
        # 日历里落的是一条**全天**的重复事件，而你以为写了时刻。
        # 2026-10-04 实测踩到（写使用说明时才发现，自检当时也没覆盖这条组合）。
        #
        # 日期仍由重复规则决定（"每周一"必须落在周一），这里**只取时刻**。
        _timed = whens.parse_when(body, base)
        if _timed is not None and _timed.has_time:
            _start = when.start.replace(hour=_timed.start.hour,
                                        minute=_timed.start.minute)
            when = whens.When(
                start=_start,
                end=_start + dt.timedelta(hours=1),   # 与"给了时刻"的默认时长一致
                all_day=False,
                has_date=True,        # 重复规则已经定死了是哪一天
                has_time=True,
                date_text=when.date_text,
                time_text=_timed.time_text)
    else:
        when = whens.parse_when(body, base)

    if when is None:
        raise RouteError(
            "这条日程的时间我认不出来 —— 请带时间重发一条，"
            "或改用 # 记成备忘")

    title = whens.clean_text(whens.strip_time_phrases(body))
    if not title or whens.is_bare_time(title):
        # 剥完只剩下时间（例如 `@明天 9:00`）→ 同样是没有事由
        raise RouteError(
            "这条日程只有时间、没有写是什么事 —— 请把事由一起写上")

    # ② 只说了时刻、而该时刻**今天已经过去** → 顺延到明天。
    #
    # 这条规则原本在 `intake.py` 里（已实测过），现移到这里 ——
    # 因为路由层是决定"写成什么"的唯一地方，时间语义应当在这里定死，
    # 而不是让下游各自补一遍（那正是"同一件事两处实现"的开端）。
    #
    # `whens.parse_when` 是纯函数、刻意不读时钟（否则没法离线测），
    # 并在注释里写明"由调用方决定（它本来就知道'现在'）"。
    # 调用方就是这里。
    rolled = False
    if not when.has_date and when.start < _now(base):
        shift = dt.timedelta(days=1)
        when = whens.When(
            start=when.start + shift, end=when.end + shift,
            all_day=when.all_day,
            has_date=True,          # 顺延之后**是**有明确日期的
            has_time=when.has_time,
            date_text="", time_text=when.time_text)
        rolled = True

    return Item(kind=Kind.EVENT, text=title, raw=raw, when=when,
                recurrence=recur, rolled=rolled)


def _now(base: dt.date | None) -> dt.datetime:
    """
    "现在"是什么时候。

    `base` 只在测试时传 —— 传了就用那天的 **23:59**，于是
    "今天的时刻已过"这件事在测试里是确定的（否则结果随运行时刻变化，
    断言会变成薛定谔的）。
    """
    if base is not None:
        return dt.datetime.combine(base, dt.time(23, 59))
    return dt.datetime.now()


def main() -> int:
    """命令行自检：把一批样例过一遍路由。"""
    import argparse

    ap = argparse.ArgumentParser(description="按符号路由（纯函数，不写入）")
    ap.add_argument("text", nargs="+", help="要路由的文本")
    ap.add_argument("--base", help="基准日期 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    base = dt.date.fromisoformat(args.base) if args.base else None
    for t in args.text:
        print(f"{t!r}")
        try:
            it = route(t, base)
        except RouteError as e:
            print(f"    ❌ 拒绝：{e}")
            continue
        line = f"    → {it.kind.value}：{it.text!r}"
        if it.when is not None:
            line += f"  @ {whens.format_when(it.when)}"
        if it.recurrence:
            line += f"  [{it.recurrence}]"
        print(line)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
