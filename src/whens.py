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


# 整句只是一个时间表达式时用得上：日期 + 时段 + 时刻，中间允许少量填充词。
# 刻意**不**允许出现别的内容 —— 判据是"整句就是时间"，不是"句子里有时间"。
_BARE_TIME = re.compile(
    r"^\s*"
    rf"(?:(?:{_PERIOD_RE})\s*)?"
    r"(?:(?:\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*[日号]?"
    r"|(?<!\d)\d{1,2}\s*[-/.月]\s*\d{1,2}\s*[日号]?"
    r"|大后天|大前天|后天|前天|明天|明日|今日|今天|昨天"
    r"|(?:下下|下|这|本|上)?\s*(?:周|星期|礼拜)\s*[一二三四五六日天1234567])"
    r"\s*(?:的|,|，)?\s*)?"
    rf"(?:(?:{_PERIOD_RE})\s*)?"
    r"(?:"
    r"(?<!\d)\d{1,2}\s*[:：]\s*\d{2}(?:\s*[:：]\s*\d{2})?(?!\d)"
    r"|(?<!\d)(?:[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*[点时時]\s*"
    r"(?:半|[0-9]{2}|[零〇一二两三四五六七八九十]{1,3})?\s*分?"
    r")?"
    r"\s*$"
)


def is_bare_time(text: str, base: dt.date | None = None) -> bool:
    """
    整句**只是一个时间表达式**吗（"上午九点"／"明天下午两点"／"9:00"）？

    与"句子里含时间"是两回事：
        "上午九点"        → True   （没有内容）
        "上午九点开会"     → False  （内容 = 开会）
        "下午两点 项目周会" → False  （内容 = 项目周会）

    ## 为什么需要这个判据（实测踩到的坑）

    用户被追问"这条日程没写时间"时，最自然的回答就是"上午九点"。
    而 system 当时把这种回答当成**一条新的日程**：标题变成「上午九点」，
    时间落在今天 09:00（已经过去），原来那条的事由丢了。
    有了这个判据，收件层就能认出"这是用户在补时间"，从而接到上一条上。

    ## 为什么放在本模块

    时间词汇表（时段词、日期词、时刻写法）都在这里。把判据写在别处
    就得复制一份正则 —— 而"复制一份词汇表"正是这个项目反复踩的坑。

    ## 曾经的错解（记下来免得再走一遍）

    先试的是"把命中片段从正文里剥掉，看还剩什么"。**不可靠**：
    `When.time_text` 只记录命中片段，"上午九点"里它只抓到"九点"，
    "上午"留在残余里；而"上午"自己又能解析成默认 9:00，
    于是判据说"这句有内容"—— 修复等于没生效，标题依旧是「上午九点」。
    改用"整句全匹配"才绕开"片段记不全"这个坑。
    """
    src = (text or "").strip()
    if not src:
        return False
    return _BARE_TIME.match(src) is not None


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
    #
    # ⚠️ 而**全天**（连时段都没说，如"明天"）必须从 **00:00** 起：
    # 原先这里一律用 `default_hour`（9 点），只把 all_day 标志和时长改掉 ——
    # 于是"全天"事件实际是"当天 09:00 + 24 小时"，落到日历里跨了**两天**
    # （用户 2026-10-05 实报："去龙井村"被排到 5 日和 6 日）。
    # 这里归零不会丢任何信息：`all_day` 为真时 `hour` 一定是默认值，
    # 从不是你说的时间（你说了时刻或时段，all_day 就不会为真）。
    all_day = (date_part is not None) and (time_part is None)
    if all_day:
        start = dt.datetime.combine(d, dt.time(0, 0))
    else:
        start = dt.datetime.combine(d, dt.time(hour, minute))

    # 注：只给了时刻、且该时刻已过时"顺延到明天"的处理**不在这里做** ——
    # 那需要读当前时钟，会破坏本模块的可测试性（纯函数）。
    # 由调用方决定（它本来就知道"现在"）。

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


def format_when(w: When, all_day_note: bool = True) -> str:
    """
    给人看的时间描述（回执里用）。

    `all_day_note=False` 时不写"（全天）"：**待办**的"时间"只是备注里的一句提示，
    它本来就没有到期日（见 `intake._real_add_todo`），说"全天"会让人以为会到期提醒。
    （日历用它出"（全天）"是对的 —— 那里真的是一条全天日程。）
    """
    wd = "一二三四五六日"[w.start.isoweekday() - 1]
    if w.all_day:
        return (f"{w.start.month}月{w.start.day}日 周{wd}"
                + ("（全天）" if all_day_note else ""))
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


# ════════════════════════════════════════════════════════════════════════
# 重复规则（2026-10-03 从 classify.py 搬来）
#
# **为什么归这里**：本模块是"时间词汇的归属地"。重复规则（每周一、每天）
# 是**时间字面量解析**，跟"这条消息属于哪个 App"无关 ——
# 后者已由符号声明（见 docs/SYMBOL-SCHEME.md），不再由代码推断。
#
# 判断标准：凡是从字符串里认出时间/日期/重复规则的，都归本模块；
# 凡是猜语义的，已全部删除。
# ════════════════════════════════════════════════════════════════════════

_RECUR_WEEKLY = re.compile(
    r"(每|各)\s*(?:周|星期|礼拜)\s*([一二三四五六日天1234567])")
_RECUR_DAILY = re.compile(r"(每天|每日)")
_RECUR_MONTHLY = re.compile(r"(每月|每个?月)\s*(\d{1,2})\s*[日号]")

# RRULE 里 BYDAY 用的两个字母 → isoweekday
_BYDAY_NUM = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 7}


def rrule_text(rrule: str) -> str:
    """
    把 RRULE 说成人话（回执 / 日报 / `/list` 里别让用户看 `FREQ=WEEKLY`）。

    原本在 `intake._recurrence_text` 里，2026-10-05 搬到 here ——
    因为**读取路径也要用它**了（重复日程按天出现时要标出"这条是每天的"），
    而"RRULE ↔ 人话"属于时间词汇，归 `whens`。
    """
    rrule = (rrule or "").strip()
    if not rrule:
        return ""
    if "BYDAY=MO,TU,WE,TH,FR" in rrule:
        return "每个工作日"
    if "FREQ=DAILY" in rrule:
        return "每天"
    if "FREQ=WEEKLY" in rrule:
        day = rrule.split("BYDAY=", 1)[1].split(";")[0] if "BYDAY=" in rrule else ""
        name = {"MO": "一", "TU": "二", "WE": "三", "TH": "四",
                "FR": "五", "SA": "六", "SU": "日"}.get(day, "")
        if not name and "BYDAY=" in rrule:
            # 多个星期（MO,WE）→ 逐个说
            names = [{"MO": "一", "TU": "二", "WE": "三", "TH": "四",
                      "FR": "五", "SA": "六", "SU": "日"}.get(d, "")
                     for d in day.split(",")]
            names = [n for n in names if n]
            if names:
                return "每周" + "、".join(names)
        return f"每周{name}" if name else "每周"
    if "FREQ=MONTHLY" in rrule:
        dom = rrule.split("BYMONTHDAY=", 1)[1].split(";")[0] if "BYMONTHDAY=" in rrule else ""
        return f"每月{dom}日" if dom else "每月"
    if "FREQ=YEARLY" in rrule:
        return "每年"
    return rrule


# 这些 RRULE 部件我们**看不懂** —— 见到就返回 None（调用方据此退回旧行为
# 并记日志，而不是悄悄漏掉）。"
# "每月第二个周二"（BYSETPOS）、"每年 10 月"（BYMONTH）这类，
# 要看懂就得写一个完整的 RRULE 引擎，而那是另一个量级的工程。
_RRULE_UNSUPPORTED = ("BYSETPOS", "BYMONTH=", "BYYEARDAY", "BYWEEKNO",
                      "BYHOUR", "BYMINUTE", "BYSECOND")


def _parse_rrule(rrule: str) -> dict | None:
    """RRULE → 结构化判据。看不懂返回 None（不是空字典 —— 两者含义不同）。"""
    r = (rrule or "").strip().upper()
    if not r or "FREQ=" not in r:
        return None
    if any(u in r for u in _RRULE_UNSUPPORTED):
        return None
    parts: dict = {}
    for chunk in r.split(";"):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    freq = parts.get("FREQ", "")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    try:
        interval = max(1, int(parts.get("INTERVAL", "1")))
        count = int(parts["COUNT"]) if "COUNT" in parts else None
    except ValueError:
        return None
    until = None
    if "UNTIL" in parts:
        u = parts["UNTIL"].split("T", 1)[0]
        try:
            until = dt.date(int(u[:4]), int(u[4:6]), int(u[6:8]))
        except (ValueError, IndexError):
            return None
    # ⚠️ BYDAY 必须转成**整数**（1=周一…7=周日）：存成 "MO" 这种字符串，
    # 后面拿 `target.isoweekday()`（整数）去比就永远不相等 ——
    # 结果是**所有"每周X"都判成不发生**（写完第一版时实测踩到）。
    # ⚠️ 用文件里已有的 `_BYDAY_NUM`（RRULE 的 BYDAY → 数字），
    # **不要**另起一个常量：第一版我叫它 `_WEEKDAY_NUM`，而那个名字
    # 上面已经属于"中文星期 → 数字"（`{"一":1,…}`），于是把它覆盖掉，
    # 后果是"周三""下周三"整类中文星期**解析不出来** —— 被自检第 ⑤ 组抓到。
    byday = [_BYDAY_NUM[d] for d in parts.get("BYDAY", "").split(",")
             if d in _BYDAY_NUM]
    doms = []
    for x in parts.get("BYMONTHDAY", "").split(","):
        if x.lstrip("-").isdigit():
            doms.append(int(x))
    return {"freq": freq, "interval": interval, "count": count,
            "until": until, "byday": byday, "bymonthday": doms}


def _hits(rule: dict, anchor: dt.date, target: dt.date) -> bool:
    """不含 COUNT/UNTIL 的频率判据：这一天在不在节奏上。"""
    freq, interval = rule["freq"], rule["interval"]
    if freq == "DAILY":
        return (target - anchor).days % interval == 0
    if freq == "WEEKLY":
        days = rule["byday"] or [anchor.isoweekday()]
        if target.isoweekday() not in days:
            return False
        a_mon = anchor - dt.timedelta(days=anchor.isoweekday() - 1)
        t_mon = target - dt.timedelta(days=target.isoweekday() - 1)
        return ((t_mon - a_mon).days // 7) % interval == 0
    if freq == "MONTHLY":
        doms = rule["bymonthday"] or [anchor.day]
        if target.day not in doms:
            return False
        months = (target.year - anchor.year) * 12 + (target.month - anchor.month)
        return months >= 0 and months % interval == 0
    # YEARLY
    years = target.year - anchor.year
    return (target.month == anchor.month and target.day == anchor.day
            and years >= 0 and years % interval == 0)


def occurs_on(rrule: str, anchor: dt.date, target: dt.date) -> bool | None:
    """
    这条重复规则在 `target` 那天会发生吗？（`anchor` = 事件第一次发生的日期）

    返回 **None 表示"这条规则我看不懂"** —— 调用方据此退回"只在首次那天显示"
    并记一行日志，而不是悄悄漏掉（本项目最忌讳的失败形态）。

    ## 为什么需要它

    日历里一条重复事件是**一个**对象，它的 `start date` 是**首次**发生日。
    所以按日期窗口查（`whose start date ≥ dFrom and …`）只能查到它首次那天 ——
    `@每天八点 跑步` 从此在 `/list` 与日报里再也看不见
    （2026-10-05 发现；实测 `whose recurrence is not missing value`
    会报 -1700，AppleScript 这一层**筛不出来**，只能在 Python 侧判断）。

    ## 支持到哪

    支持我们生成的 + 日历界面常见的那些：DAILY / WEEKLY / MONTHLY / YEARLY，
    可带 INTERVAL、BYDAY、BYMONTHDAY、COUNT、UNTIL。
    看不懂的（BYSETPOS / BYMONTH / BYYEARDAY …）返回 None，由调用方兜底。
    """
    rule = _parse_rrule(rrule)
    if rule is None:
        return None
    if target < anchor:
        return False
    if rule["until"] is not None and target > rule["until"]:
        return False
    if not _hits(rule, anchor, target):
        return False
    if rule["count"] is None:
        return True
    # COUNT：从 anchor 数到 target（含）为止发生了几次，超了就不算
    n, d = 0, anchor
    while d <= target:
        if _hits(rule, anchor, d):
            n += 1
        d += dt.timedelta(days=1)
    return n <= rule["count"]


def parse_recurrence(text: str) -> str:
    """
    解析周期性表达，返回 iCal RRULE 片段。

    日历的 `recurrence` 属性接受 iCal RRULE 字符串 —— 这是日历
    相对提醒事项的独特能力（提醒事项的脚本接口无 repeat，
    所以"每周一交周报"这类只有日历能做到）。
    """
    m = _RECUR_WEEKLY.search(text)
    if m:
        wd = {"一": "MO", "二": "TU", "三": "WE", "四": "TH", "五": "FR",
              "六": "SA", "日": "SU", "天": "SU",
              "1": "MO", "2": "TU", "3": "WE", "4": "TH", "5": "FR",
              "6": "SA", "7": "SU"}.get(m.group(2))
        if wd:
            return f"FREQ=WEEKLY;BYDAY={wd}"

    if _RECUR_DAILY.search(text):
        return "FREQ=DAILY"

    m = _RECUR_MONTHLY.search(text)
    if m:
        return f"FREQ=MONTHLY;BYMONTHDAY={int(m.group(2))}"

    if "每个工作日" in text or "每工作日" in text:
        return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"

    return ""


def first_occurrence(rrule: str, base: dt.date) -> "When":
    """
    从重复规则推出**下一次发生**的日期，用作日历事件的起始时间。

    为什么需要：日历事件必须有 start date，而"每周一交周报"这类说法
    没写"从哪天开始"。取下一次发生日最符合直觉（今天就是周一则用今天）。

    时刻默认 09:00（与只给日期时的默认一致），全天 —— 周期表达通常
    不带具体时刻。

    （与 `parse_recurrence` 配套：只解析出规则而不管起始日，
      日历里会落在一个奇怪的日子上。）
    """
    d = base

    if "BYDAY=" in rrule:
        days = rrule.split("BYDAY=", 1)[1].split(";", 1)[0].split(",")
        targets = sorted(_BYDAY_NUM[x] for x in days if x in _BYDAY_NUM)
        if targets:
            for i in range(8):          # 最多看一周
                cand = base + dt.timedelta(days=i)
                if cand.isoweekday() in targets:
                    d = cand
                    break
    elif "BYMONTHDAY=" in rrule:
        try:
            dom = int(rrule.split("BYMONTHDAY=", 1)[1].split(";", 1)[0])
        except ValueError:
            dom = 1
        for month_offset in (0, 1):
            y, m = base.year, base.month + month_offset
            if m > 12:
                y, m = y + 1, m - 12
            try:
                cand = dt.date(y, m, dom)
            except ValueError:
                continue            # 该月没有这一天（如 31 日）
            if cand >= base:
                d = cand
                break
    # FREQ=DAILY 无需调整：就是今天

    # ⚠️ 从 **00:00** 起，不是 9 点 —— 同 parse_when 的处理：
    # 全天事件带非零时刻会跨两天（2026-10-05 实报）。
    # 载荷里若明说了时刻，由 routes 覆盖（它会把时刻取过来）。
    start = dt.datetime.combine(d, dt.time(0, 0))
    return When(start=start, end=start + dt.timedelta(days=1),
                all_day=True, has_date=True, has_time=False,
                date_text="", time_text="")


# 时间短语本身不构成正文，但**只在它独立出现时才剥** ——
# 不能把"10月5日评审"剥成"评审"就丢了日期线索（日期已在 When 里）。
# 这里保守处理：只剥句首的时间词，句中保留（避免误伤"下周一交周报"的"周报"）。
_LEADING_TIME = re.compile(
    r"^\s*(?:"
    # 周期表达（要放在"周X"之前，"每个工作日"才不会被"每个"半途匹配）
    r"(?:每|各)\s*(?:个)?\s*工作日"
    r"|(?:每周|每星期|每个?礼拜)\s*[一二三四五六日天1234567]"
    r"|(?:每天|每日|每周|每月|每个?月)"
    r"|(?:每|各)\s*(?:周|星期|礼拜)\s*[一二三四五六日天1234567]"
    # 相对日
    r"|(?:今天|明天|后天|大后天|昨天|前天|今日|明日)"
    # 星期
    r"|(?:下下|下|这|本|上)?\s*(?:周|星期|礼拜)\s*[一二三四五六日天1234567]"
    # 裸日期（"1日""15号"，通常跟在"每月"之后）
    r"|(?:\d{1,2}\s*[日号])"
    # 绝对日期
    r"|(?:\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*[日号]?)"
    r"|(?:\d{1,2}\s*[-/.月]\s*\d{1,2}\s*[日号])"
    # 时刻
    r"|(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)"
    r"|(?:\d{1,2}\s*[:：]\s*\d{2})"
    r"|(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*[点时時]\s*(?:半|\d{2}|[零〇一二两三四五六七八九十]{1,3})?\s*分?"
    r")[\s，,、]*")


def strip_time_phrases(s: str) -> str:
    """
    剥掉句首的时间短语，得到"事情本身"。

    只剥**句首连续出现**的时间词，不做全局替换 ——
    全局替换会把"周报""月会"这类词里的字误伤掉。

    ⚠️ **这一步最容易被漏掉**。它原本藏在 `classify.py` 里，
    而 `classify.py` 的分类逻辑已整体删除。若只删不搬，标题会变成
    "周五下午两点 项目周会"（时间词混在标题里）—— 实测过这个后果。

    `@周五下午两点 项目周会` → `项目周会`
    """
    prev = None
    out = s
    while out != prev:
        prev = out
        out = _LEADING_TIME.sub("", out, count=1)
    return out.strip() or s


def clean_text(s: str) -> str:
    """清理正文：折叠空白、去首尾标点。（同样从 classify.py 搬来）"""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.strip("，,。.、；;：:!！?？ ")
    return s


if __name__ == "__main__":
    import sys
    sys.exit(main())
