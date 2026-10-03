#!/usr/bin/env python3
"""
改分类：把记错类型的东西移到正确的地方。

## 为什么需要它

实测踩到：用户发了一段感慨（"真正要学的是原理和工程思想…"），
判定器把它当成了**待办**写进提醒事项。用户接着说「改为备忘录」——
而当时的系统把这句话当成了**新内容**，于是新建了一条叫
"改为备忘录"的备忘，原条目仍留在提醒事项。**纠正通道根本不存在。**

## 怎么做到"不猜"

v4 的核心教训是"不要猜身份"（行号/原样文字/跨天同一条/⟳ 标记，
四次同源 bug）。所以定位目标有两条路径：

    A. **回复我的回执** → Telegram 给 reply_to_message.message_id
       → 我在 journal 里记过"我发出的回执 id" → **精确定位，零猜测**
    B. 直接发短指令 → 落到"最近一条" → **必须先回显确认**再动

路径 B 是便利性补充，且**绝不静默执行** —— 隐式指代一旦猜错，
动的就是用户 Apple 应用里的真实数据。

## 关于"删除"

改分类必然要把原条目从旧 App 移走，所以本模块需要删除能力 ——
这违反了 v4 最初"agent 不删任何东西"的约束。开口子的代价用三条限制控制：

    ① 只用明确的 id 删（绝不按标题/内容模糊匹配）
    ② 只有本模块调用（其他路径不碰 delete）
    ③ 每次删除都记 journal，可追溯
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import journal          # noqa: E402
from classify import Kind  # noqa: E402

# 目标类型的中文说法 → Kind
_TARGET_WORDS: dict[str, Kind] = {
    "待办": Kind.TODO, "任务": Kind.TODO, "提醒": Kind.TODO, "todo": Kind.TODO,
    "日程": Kind.EVENT, "日历": Kind.EVENT, "会议": Kind.EVENT,
    "event": Kind.EVENT,
    "备忘": Kind.MEMO, "备忘录": Kind.MEMO, "笔记": Kind.MEMO, "memo": Kind.MEMO,
}

# 纠正动词
_CHANGE_VERBS = ("改为", "改成", "改到", "应该是", "不是", "放到", "移到",
                 "归到", "算作", "当作", "换为", "换成")
# "不是"单独出现时是"删除/排除"的意思，需要区分
_NEGATE = ("不是", "不对", "不用记", "删除", "删掉", "取消", "去掉", "别记",
           "撤掉", "不要记")


class Correction:
    """解析出的纠正意图。"""
    __slots__ = ("target", "drop", "raw")

    def __init__(self, target: Kind | None, drop: bool, raw: str):
        self.target = target
        self.drop = drop          # True = 不是要记的东西，撤掉
        self.raw = raw

    def __repr__(self) -> str:
        if self.drop:
            return "Correction(drop)"
        return f"Correction(target={self.target})"


def parse_correction(text: str) -> Correction | None:
    """
    判断这是不是一个纠正指令。不是则返回 None。

    判据要**严**：只有明确出现"改/换成/不是/删掉 + 目标类型"才算，
    否则一句正常的话（"这个改成那样比较好"）会被误当纠正。
    所以还要求整句较短（短指令才是纠正）。
    """
    t = (text or "").strip()
    if not t or len(t) > 20:
        return None

    # 明确否定 → 撤掉（"不是待办"、"删掉这条"）
    if any(n in t for n in _NEGATE):
        # "不是"后面跟类型词 → 撤掉那条
        if any(w in t for w in _TARGET_WORDS):
            return Correction(None, True, t)
        if any(n in t for n in ("删除", "删掉", "取消", "去掉", "别记",
                                "不用记", "撤掉", "不要记")):
            return Correction(None, True, t)

    # "改为X" / "换成X" / "应该是X"
    if any(v in t for v in _CHANGE_VERBS):
        for word, kind in _TARGET_WORDS.items():
            if word in t:
                return Correction(kind, False, t)

    return None


def describe_target(kind: Kind) -> str:
    return {Kind.TODO: "待办（提醒事项）",
            Kind.EVENT: "日程（日历）",
            Kind.MEMO: "备忘（备忘录）"}[kind]


def describe_origin(rec: dict) -> str:
    """从 journal 记录看它原本是什么、正文是什么。"""
    ev = rec.get("event")
    if ev == journal.EV_TODO_ADDED:
        return "待办（提醒事项）", rec.get("text", "")
    if ev == journal.EV_EVENT_ADDED:
        return "日程（日历）", rec.get("summary", "")
    if ev == journal.EV_MEMO_ADDED:
        return "备忘（备忘录）", rec.get("text", "")
    return "未知", ""


def plan(rec: dict, target: Kind | None, drop: bool) -> str:
    """
    生成"将要做什么"的人话说明（**用于回显确认**）。

    刻意不执行任何操作 —— 先让用户看清再动。
    """
    origin, text = describe_origin(rec)
    short = text if len(text) <= 24 else text[:24] + "…"
    if drop:
        return f"把「{short}」从 {origin} 撤掉（不再记录）"
    return f"把「{short}」从 {origin} 改为 {describe_target(target)}"


# ── 执行

def _remove_from(rec: dict, notes) -> tuple[bool, str]:
    """从原归属里移走。返回 (是否成功, 说明)。"""
    ev = rec.get("event")

    if ev == journal.EV_TODO_ADDED:
        rid = rec.get("reminder_id") or ""
        if not rid:
            return False, "原记录里没有 reminder_id，无法安全定位"
        import reminders
        rem = reminders.Reminders()
        ok = rem.delete(rid)
        return ok, "已从提醒事项移除" if ok else "从提醒事项移除失败"

    if ev == journal.EV_EVENT_ADDED:
        uid = rec.get("uid") or ""
        if not uid:
            return False, "原记录里没有 uid，无法安全定位"
        import applecal
        ok = applecal.delete(uid)
        return ok, "已从日历移除" if ok else "从日历移除失败"

    if ev == journal.EV_MEMO_ADDED:
        nid = rec.get("memo_id") or ""
        if not nid:
            return False, "原记录里没有 memo_id，无法安全定位"
        import memo
        ok = memo.delete(nid)
        return ok, "已从备忘录移除" if ok else "从备忘录移除失败"

    return False, f"未知来源类型：{ev}"


def _add_to(kind: Kind, text: str, notes) -> tuple[bool, str]:
    """加到新归属里。返回 (是否成功, 说明)。"""
    import intake
    it = intake.Intake()

    if kind is Kind.TODO:
        ref = it.add_todo(text)
        return True, f"已记入提醒事项（{ref[:40]}）"

    if kind is Kind.MEMO:
        ref = it.add_memo(text)
        return True, f"已记入备忘录（{ref[:40]}）"

    if kind is Kind.EVENT:
        # 日程需要时间。原文本里若没有时间，**不猜**，让用户补充。
        import whens
        w = whens.parse_when(text)
        if w is None:
            return False, "改成日程需要时间（比如「周五下午两点」），请带时间再说一次"
        ref = it.add_event(text, w.start, w.end, allday=w.all_day)
        return True, f"已记入日历（{ref[:40]}）"

    return False, f"未知目标类型：{kind}"


def apply(rec: dict, target: Kind | None, drop: bool,
          dry_run: bool = False) -> tuple[bool, str]:
    """
    执行改分类。返回 (是否成功, 说明)。

    顺序刻意是"**先加新的、再删旧的**"：
    万一中途失败，最坏情况是"两处都有"（看得见、可手动清理），
    而反过来会变成"两处都没有"（数据静默丢失 —— 本项目最忌讳的失败模式）。
    """
    origin, text = describe_origin(rec)
    if not text:
        return False, "原记录里没有正文，无法迁移"

    if dry_run:
        return True, f"【干跑】{plan(rec, target, drop)}"

    notes: list[str] = []

    if not drop:
        ok, msg = _add_to(target, text, notes)
        notes.append(msg)
        if not ok:
            # 新位置没建成 → **不删旧的**，避免数据丢失
            return False, "；".join(notes) + "（原条目未动）"

    ok, msg = _remove_from(rec, notes)
    notes.append(msg)
    if not ok:
        return False, "；".join(notes) + "（可能两处都有了，请手动清理）"

    return True, "；".join(notes)
