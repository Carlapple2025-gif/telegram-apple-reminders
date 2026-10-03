#!/usr/bin/env python3
"""
把中文自然语言里的时间解析成具体日期时间。

## 为什么单独成模块

AppleScript 的日期字面量受**系统区域设置**影响，直接拼字符串会出现
"差一天""差 8 小时"这类极难发现的错误（时区/夏令时/区域格式）。

对策：时间解析**全部在这里用纯 Python 完成**，AppleScript 只接收
已经算好的年、月、日、时、分五个整数，用 `set` 逐个赋值构造日期。
这样时间语义与 AppleScript 完全解耦，且可以彻底离线测试。

## 纯函数设计

`parse_when(text, base)` 的 `base` 是"今天"，由调用方传入 ——
不读系统时钟，所以**测试可以固定任意一天**，不会出现"测试今天过、
明天挂"的问题。

## 支持的说法（都是实际会用到的）

日期：
    今天 / 明天 / 后天 / 大后天
    周五 / 星期五 / 礼拜五 / 这周五 / 下周五 / 下下周五
    10月5日 / 10月5号 / 10/5 / 2026-10-05 / 2026年10月5日
时间：
    14:00 / 14点 / 14点30 / 下午两点 / 下午2点 / 晚上8点半
    早上9点 / 中午12点 / 凌晨1点
时段（无具体时刻）：
    上午 / 早上 → 09:00    中午 → 12:00
    下午 → 14:00           晚上 → 20:00
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# ── 中文数字

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def cn_number(s: str) -> int | None:
    """
    解析中文数字，支持 1-59 的常见写法。

    覆盖：一~十、十一~十九、二十、二十一~二十九、三十、三十一…
    不做通用大数解析 —— 时间场景用不到，多余复杂度是负担。
    """
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)

    if "十" not in s:
        # 单个汉字，或"两"这类
        return _CN_DIGITS.get(s)

    # 形如 十 / 十五 / 二十 / 二十五 / 三十
    head, _, tail = s.partition("十")
    tens = _CN_DIGITS.get(head, 1) if head else 1
    ones = _CN_DIGITS.get(tail, 0) if tail else 0
    if head and head not in _CN_DIGITS:
        return None
    if tail and tail not in _CN_DIGITS:
        return None
    return tens * 10 + ones


# ── 日期部分

_DATE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 绝对日期：2026-10-05 / 2026/10/5 / 2026.10.5
    ("abs_ymd", re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?")),
    # 月日：10月5日 / 10月5号 / 10-5（不带年）
    ("md", re.compile(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?(?!\d)")),
    # 相对日
    ("rel", re.compile(r"(大后天|大前天|后天|前天|明天|明天|今日|今天|昨天|昨天)")),
    # 星期：下下周五 / 下周五 / 这周五 / 周五 / 星期五 / 礼拜五
    ("weekday", re.compile(
        r"(下下|下|这|本|上)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1234567])")),
]

_WEEKDAY_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
                "日": 7, "天": 7, "1": 1, "2": 2, "3": 3, "4": 4,
                "5": 5, "6": 6, "7": 7}

_REL_OFFSET = {
    "大前天": -3, "前天": -2, "昨天": -1, "今天": 0, "今日": 0,
    "明天": 1, "明日": 1, "后天": 2, "大后天": 3,
}


# ── 时间部分

# 时段词 → 默认小时（当只说了"下午"没说几点时使用）
PERIOD_DEFAULT_HOUR = {
    "凌晨": 5, "早上": 8, "早晨": 8, "上午": 9, "中午": 12,
    "下午": 14, "傍晚": 17, "晚上": 20, "夜里": 21, "晚": 20,
}

# 时段 → 该时段的小时范围（用于判断 12 小时制是否要 +12）
PERIOD_RANGE = {
    "凌晨": (0, 5), "早上": (5, 11), "早晨": (5, 11), "上午": (5, 11),
    "中午": (11, 13), "下午": (12, 18), "傍晚": (16, 19),
    "晚上": (17, 24), "夜里": (18, 24), "晚": (17, 24),
}

_PERIOD_RE = "|".join(sorted(PERIOD_DEFAULT_HOUR, key=len, reverse=True))

# 14:30 / 14:30:00
_HHMM = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?(?!\d)")
# 14点 / 14点半 / 14点30 / 14时30分 / 两点半
_HOUR_CN = re.compile(
    r"(?<!\d)([0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*[点时時]\s*"
    r"(半|[0-9]{2}|[零〇一二两三四五六七八九十]{1,3})?\s*分?")
# 单独一个"下午两点"里的"两点"（没有"点"字的情况较少，不处理，避免误判数字）


@dataclass
class When:
    """解析结果。"""
    start: dt.datetime
    end: dt.datetime
    all_day: bool = False
    has_date: bool = False      # 说了日期吗
    has_time: bool = False      # 说了具体时刻吗
    date_text: str = ""         # 命中的原文（便于回显给用户确认）
    time_text: str = ""


def parse_when(text: str, base: dt.date | None = None,
               default_hour: int = 9) -> When | None:
    """
    从文本里解析时间。解析不出返回 None（调用方据此判定"不是日程"）。

    `base` 为 None 时用今天 —— 但**测试请显式传入**，否则结果随日期变化。
    """
    if not text or not text.strip():
        return None
    base = base or dt.date.today()
    src = text.strip()

    date_part = _find_date(src, base)
    time_part = _find_time(src)

    if date_part is None and time_part is None:
        return None

    has_date = date_part is not None

    if date_part is None:
        # 只说了时间（如"下午两点"）→ 默认今天；
        # 若该时刻已过，挪到明天（"下午两点开会"在晚上说，指的是明天）
        d = base
    else:
        d = date_part[0]

    if time_part is not None:
        hour, minute, has_time, time_text = time_part
    else:
        hour, minute, has_time, time_text = default_hour, 0, False, ""

    # 只说了时段（如"上午"）→ 算全天还是定时？
    # 这里按"定时"处理更符合直觉（"明天上午交电费"→ 9:00），
    # 但标记 has_time=False，让调用方知道时刻是推断的。
    start = dt.datetime.combine(d, dt.time(hour, minute))

    # 注：只给了时刻、且该时刻已过时"顺延到明天"的处理**不在这里做** ——
    # 那需要读当前时钟，会破坏本模块的可测试性（纯函数）。
    # 由调用方决定（它本来就知道"现在"）。

    all_day = (date_part is not None) and (time_part is None)
    end = start + (dt.timedelta(days=1) if all_day else dt.timedelta(hours=1))

    return When(
        start=start, end=end, all_day=all_day,
        has_date=has_date, has_time=has_time,
        date_text=date_part[1] if date_part else "",
        time_text=time_text,
    )


def _find_date(src: str, base: dt.date) -> tuple[dt.date, str] | None:
    """按优先级找日期。返回 (日期, 命中的原文)。"""
    # 绝对日期优先（最明确）
    for kind, pat in _DATE_PATTERNS:
        m = pat.search(src)
        if not m:
            continue

        if kind == "abs_ymd":
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return dt.date(y, mo, d), m.group(0)
            except ValueError:
                return None      # 2月30日 这类非法日期

        if kind == "md":
            mo, d = int(m.group(1)), int(m.group(2))
            if not (1 <= mo <= 12 and 1 <= d <= 31):
                continue
            # 不带年 → 用 base 的年；若已过去则算明年
            for y in (base.year, base.year + 1):
                try:
                    cand = dt.date(y, mo, d)
                except ValueError:
                    break
                if cand >= base:
                    return cand, m.group(0)
            continue

        if kind == "rel":
            off = _REL_OFFSET.get(m.group(1))
            if off is not None:
                return base + dt.timedelta(days=off), m.group(0)
            continue

        if kind == "weekday":
            prefix, wd = m.group(1) or "", m.group(2)
            target = _WEEKDAY_NUM.get(wd)
            if target is None:
                continue
            delta = (target - base.isoweekday()) % 7
            if prefix == "下下":
                delta += 14
            elif prefix == "下":
                delta += 7 if delta > 0 else 7   # 下周五必然是下周
                delta = ((target - base.isoweekday()) % 7) + 7
            elif prefix in ("上",):
                delta = ((target - base.isoweekday()) % 7) - 7
            elif prefix in ("这", "本"):
                pass          # 本周内，可能已过（保留，由调用方判断）
            # 无前缀："周五" → 最近的将来那个（今天就是则今天）
            return base + dt.timedelta(days=delta), m.group(0)

    return None


def _find_time(src: str) -> tuple[int, int, bool, str] | None:
    """
    找时间。返回 (小时, 分钟, 是否有具体时刻, 命中的原文)。

    优先级：HH:MM > N点M分 > 单说时段词。
    """
    # ① HH:MM
    m = _HHMM.search(src)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            # 12 小时制补正：结合上下文里的时段词
            h = _apply_period(h, src)
            return h, mi, True, m.group(0)

    # ② N点 / N点半 / N点M分（含中文数字）
    m = _HOUR_CN.search(src)
    if m:
        h = cn_number(m.group(1))
        if h is not None and 0 <= h <= 24:
            tail = m.group(2)
            if tail == "半":
                mi = 30
            elif tail:
                mi = cn_number(tail)
                mi = 0 if mi is None else mi
            else:
                mi = 0
            if 0 <= mi <= 59:
                h = _apply_period(h, src)
                if h == 24:
                    h = 0
                return h, mi, True, m.group(0)

    # ③ 只说了时段词
    m = re.search(_PERIOD_RE, src)
    if m:
        word = m.group(0)
        return PERIOD_DEFAULT_HOUR[word], 0, False, word

    return None


def _apply_period(hour: int, src: str) -> int:
    """
    结合时段词把 12 小时制补成 24 小时制。

    "下午两点" → 14；"晚上8点" → 20；"早上9点" → 9（不变）。
    已经 ≥ 13 的不动（用户说了 24 小时制）。
    """
    if hour >= 13:
        return hour
    for word, (lo, hi) in PERIOD_RANGE.items():
        if word in src and lo >= 12 and hour < 12:
            return hour + 12
    # "中午12点" 特例：12 点就是 12，不 +12
    if "中午" in src and hour == 12:
        return 12
    return hour


def format_when(w: When) -> str:
    """给人看的时间描述（回执里用）。"""
    wd = "一二三四五六日"[w.start.isoweekday() - 1]
    if w.all_day:
        return f"{w.start.month}月{w.start.day}日 周{wd}（全天）"
    return (f"{w.start.month}月{w.start.day}日 周{wd} "
            f"{w.start.hour:02d}:{w.start.minute:02d}")


def main() -> int:
    """命令行自检：解析一批样例。"""
    import argparse
    ap = argparse.ArgumentParser(description="中文时间解析（纯函数）")
    ap.add_argument("text", nargs="+", help="要解析的文本")
    ap.add_argument("--base", help="基准日期 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    base = dt.date.fromisoformat(args.base) if args.base else dt.date.today()
    print(f"基准日期：{base} 周{'一二三四五六日'[base.isoweekday()-1]}")
    print()
    for t in args.text:
        w = parse_when(t, base)
        if w is None:
            print(f"  {t!r:<28} → （无时间信息）")
        else:
            flags = []
            if w.has_date:
                flags.append("有日期")
            if w.has_time:
                flags.append("有时刻")
            if w.all_day:
                flags.append("全天")
            print(f"  {t!r:<28} → {format_when(w)}  [{'/'.join(flags) or '推断'}]")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
