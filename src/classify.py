#!/usr/bin/env python3
"""
命令分类：把一句自然语言判成 待办 / 日程 / 备忘。

## 设计原则（都来自 v1 的教训）

**判断归用户、整理归 agent** —— 所以：

  1. 明确的直接分派，不打扰你
  2. **真的不确定时才问一次**（`Confidence.ASK`）
  3. 绝不静默丢弃：判不出来就归为备忘（最轻的归属），并如实回执

v1 的时段选择器之所以失控（551 行），是因为它把"问一次"做成了
"一段有状态的对话"。这里严格守住：

    ✗ 不需要跨两次点击记住东西（除了待办本身）
    ✗ 不需要重绘整条消息体现进度
    ✗ 不需要超时/重入/确认屏

## 分类依据

| 类型 | 线索 | 例子 |
|---|---|---|
| **日程** | 有**事件名词**（会/约/聚餐…）或"某时间点发生的事" | `周五下午两点项目周会` |
| **待办** | 有**动作动词** | `交电费`、`跟进修缮` |
| **备忘** | 无动作也无事件，是"一条信息" | `想起一件事，荷载要按名称命名` |

时间信息（`whens`）是重要线索但**不是唯一判据**：
`明天交电费` 有时间也是待办，`项目周会` 没时间也是日程。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum

import whens


class Kind(str, Enum):
    TODO = "todo"
    EVENT = "event"
    MEMO = "memo"


class Confidence(str, Enum):
    """分类的可信度。ASK 表示"两种解释都说得通，问你一次"。"""
    HIGH = "high"
    LOW = "low"
    ASK = "ask"


# ── 线索词表
#
# 宁可少列也不要错列 —— 一个错的线索词会把整类句子判错，
# 而漏掉的后果只是"退化成问一次"，代价小得多。

# 事件名词：出现这些，基本是"某时间发生的事"
_EVENT_WORDS = (
    "会议", "周会", "例会", "月会", "年会", "评审", "面谈", "面试", "约见",
    "聚餐", "聚会", "饭局", "生日", "婚礼", "宴", "出差", "航班", "高铁",
    "火车", "飞机", "体检", "就诊", "挂号", "看医生", "开庭", "答辩",
    "演讲", "培训", "课程", "考试", "比赛", "演出", "演唱会", "球赛",
    # 周期性活动（"每天跑步"这类，本身是"发生的事"而非"待办的动作"）
    "跑步", "健身", "锻炼", "散步", "游泳", "瑜伽", "打球", "读书",
    "学习", "复习", "背单词", "练琴",
    "会", "约", "局",
)

# 动作动词：出现这些，基本是"要做的事"
_TODO_VERBS = (
    "交", "买", "付", "还", "寄", "发", "送", "取", "拿", "领", "办",
    "做", "写完", "写", "改", "修", "整理", "清理", "核对", "检查", "盘",
    "联系", "回复", "回", "打给", "问", "确认", "预约", "订", "报",
    "提交", "上传", "下载", "打印", "复印", "盖章", "签字", "跟进",
    "催", "提醒", "记得", "安排", "准备", "更新", "补", "续", "缴",
)

# 明确的"这是备忘"信号
_MEMO_HINTS = (
    "想起", "记一下", "记下", "备忘", "备注", "存一下", "存个",
    "灵感", "想法", "点子", "注意", "留意", "记着",
)

# 明确的"这是待办"信号
_TODO_HINTS = (
    "要", "得", "需要", "别忘了", "记得", "待办", "任务", "todo",
)

# 明确的"这是日程"信号
_EVENT_HINTS = (
    "安排", "定在", "约在", "@",
)

# 周期性表达
_RECUR_WEEKLY = re.compile(
    r"(每|各)\s*(?:周|星期|礼拜)\s*([一二三四五六日天1234567])")
_RECUR_DAILY = re.compile(r"(每天|每日)")
_RECUR_MONTHLY = re.compile(r"(每月|每个?月)\s*(\d{1,2})\s*[日号]")


@dataclass
class Classified:
    """分类结果。"""
    kind: Kind
    confidence: Confidence
    text: str                       # 清理后的正文（去掉时间/线索短语）
    raw: str                        # 原始输入
    reason: str = ""                # 为什么这样判（回执里给用户看）
    when: whens.When | None = None  # 解析出的时间（日程必有）
    recurrence: str = ""            # 重复规则（iCal RRULE 片段）
    candidates: list[Kind] = field(default_factory=list)  # ASK 时的候选

    @property
    def needs_ask(self) -> bool:
        return self.confidence is Confidence.ASK


# ── 主分类

def classify(text: str, base: dt.date | None = None) -> Classified | None:
    """
    把一句输入分类。空输入返回 None。

    判定顺序（重要 —— 顺序本身就是规则）：
        ① 明确的备忘信号 → 备忘（"想起/记一下"这类词很明确）
        ② 周期性表达     → 日程（"每周一开会"）
        ③ 事件名词       → 日程
        ④ 动作动词       → 待办
        ⑤ 有时间+无动作  → 日程
        ⑥ 都判不出       → 问一次
    """
    raw = (text or "").strip()
    if not raw:
        return None

    when = whens.parse_when(raw, base)
    recur = parse_recurrence(raw)
    # 有具体时间、或是周期表达时，都要剥掉句首的时间短语
    # （"每天跑步" → "跑步"；否则正文会把"每天"带上）
    body = _strip_time_phrases(raw) if (when or recur) else raw
    body = _clean(body)

    has_event = _has(raw, _EVENT_WORDS)
    has_todo = _has(raw, _TODO_VERBS)
    has_memo_hint = _has(raw, _MEMO_HINTS)

    # ① 明确的备忘信号（"想起一件事…"）
    if has_memo_hint and not has_event:
        return Classified(Kind.MEMO, Confidence.HIGH, body, raw,
                          reason="含「想起/记一下」这类词，判为备忘",
                          when=None)

    # ② 周期性表达 → 日程（**优先于动作动词**）
    #
    # 为什么优先：日历能设 `recurrence`（重复规则），提醒事项的脚本接口
    # **不支持**（实测其字典里没有 repeat）。所以"每周一交周报"放日历
    # 才符合它的语义 —— 放提醒事项只能建一次，不会自动重复。
    #
    # 代价：周期性的"事"会被当成日程而不是待办。这是有意的取舍 ——
    # 日历会按周期每天/每周展示，不需要"打钩"（做完就过去了）。
    if recur:
        # 周期表达通常没写"从哪天开始"，但日历事件必须有起始时间。
        # 按周期规则推出**下一次发生**的日期（今天/本周内则用今天）。
        if when is None:
            when = _first_occurrence(recur, base or dt.date.today())
        return Classified(Kind.EVENT, Confidence.HIGH, body, raw,
                          reason=f"周期性（{recur}）—— 日历支持重复规则",
                          when=when, recurrence=recur)

    # ③ 事件名词 → 日程
    if has_event:
        conf = Confidence.HIGH if when else Confidence.LOW
        reason = "含事件名词" + ("，且有时间" if when else "，但没写时间")
        return Classified(Kind.EVENT, conf, body, raw, reason=reason,
                          when=when, recurrence=recur)

    # ④ 动作动词 → 待办
    if has_todo:
        return Classified(Kind.TODO, Confidence.HIGH, body, raw,
                          reason="含动作动词，判为待办", when=None)

    # ⑤ 有时间但没动作 → 日程（"明天下午三点"本身就是个日程）
    if when and when.has_time:
        return Classified(Kind.EVENT, Confidence.LOW, body, raw,
                          reason="只给了时间点，按日程处理", when=when)

    # ⑥ 判不出 → 问一次
    return Classified(Kind.MEMO, Confidence.ASK, body, raw,
                      reason="看不出是待办、日程还是备忘",
                      candidates=[Kind.TODO, Kind.EVENT, Kind.MEMO],
                      when=when)


# ── 重复规则

def parse_recurrence(text: str) -> str:
    """
    解析周期性表达，返回 iCal RRULE 片段（不含 FREQ 前缀的组装细节）。

    日历的 `recurrence` 属性接受 iCal RRULE 字符串 —— 这是日历
    相对提醒事项的独特能力（提醒事项的脚本接口无 repeat）。
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


# ── 辅助

# RRULE 里 BYDAY 用的两个字母 → isoweekday
_BYDAY_NUM = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 7}


def _first_occurrence(rrule: str, base: dt.date) -> whens.When:
    """
    从重复规则推出**下一次发生**的日期，用作日历事件的起始时间。

    为什么需要：日历事件必须有 start date，而"每周一交周报"这类说法
    没写"从哪天开始"。取下一次发生日最符合直觉（今天就是周一则用今天）。

    时刻默认 09:00（与 whens 里只给日期时的默认一致），全天与否按
    "有没有具体时刻"决定 —— 周期表达通常没有，所以是全天。
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

    start = dt.datetime.combine(d, dt.time(9, 0))
    return whens.When(start=start, end=start + dt.timedelta(days=1),
                      all_day=True, has_date=True, has_time=False,
                      date_text="", time_text="")


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(w in text for w in words)


def _clean(s: str) -> str:
    """清理正文：折叠空白、去首尾标点。"""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.strip("，,。.、；;：:!！?？ ")
    return s


# 时间短语本身不构成正文，但**只在它独立出现时才剥** ——
# 不能把"10月5日评审"剥成"评审"就丢了日期线索（日期已在 when 里）。
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


def _strip_time_phrases(s: str) -> str:
    """
    剥掉句首的时间短语，得到"事情本身"。

    只剥**句首连续出现**的时间词，不做全局替换 ——
    全局替换会把"周报""月会"这类词里的字误伤掉。
    """
    prev = None
    out = s
    while out != prev:
        prev = out
        out = _LEADING_TIME.sub("", out, count=1)
    return out.strip() or s


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="命令分类（纯函数）")
    ap.add_argument("text", nargs="+", help="要分类的文本")
    ap.add_argument("--base", help="基准日期 YYYY-MM-DD")
    args = ap.parse_args()

    base = dt.date.fromisoformat(args.base) if args.base else dt.date.today()
    label = {Kind.TODO: "待办", Kind.EVENT: "日程", Kind.MEMO: "备忘"}
    conf = {Confidence.HIGH: "明确", Confidence.LOW: "推测", Confidence.ASK: "需确认"}

    for t in args.text:
        c = classify(t, base)
        if c is None:
            print(f"  （空）")
            continue
        when_s = f"  {whens.format_when(c.when)}" if c.when else ""
        recur_s = f"  [{c.recurrence}]" if c.recurrence else ""
        print(f"  {t!r}")
        print(f"    → {label[c.kind]}（{conf[c.confidence]}）{when_s}{recur_s}")
        print(f"      正文：{c.text!r}")
        print(f"      依据：{c.reason}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
