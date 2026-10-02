#!/usr/bin/env python3
"""
完成状态的解析与合并。

**权威来源：提醒事项的 `completed`。** 其余表达（备忘录里的 `[x]`、
Telegram 里的回复）都是输入通道，最终都归一到它。

为什么需要这个模块：
    同一条待办的状态可能出现在多处，而"哪个说了算"必须只在一处定义。
    分散在日报、顺延、同步三个脚本里各写一遍，迟早会漂移 ——
    而"三处对同一条待办的理解不一致"是这类系统最难查的 bug。

合并规则（**倾向"完成"**）：
    任一处显示已完成 → 视为完成。
    理由：打钩是个有意的动作（无论在哪儿做的），而"未打钩"往往只是没同步到
    或忘记勾。把已完成的判成未完成，会导致它被顺延到明天 —— 你会看到
    一件早就做完的事反复出现；反过来则是提前消失，最多你自己补记。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import parse as parser  # noqa: E402
from reminders import Reminders, RemindersError, Reminder, make_key, key_of  # noqa: E402


@dataclass
class ResolvedEntry:
    entry: parser.Entry
    completed: bool
    source: str          # 'reminders' | 'notes' | 'none'


@dataclass
class Resolved:
    entries: list[ResolvedEntry]
    reminders_available: bool
    reminders_error: str | None
    not_pushed: list[parser.Entry]   # 提醒事项里找不到的（尚未同步）

    @property
    def todos(self) -> list[ResolvedEntry]:
        return [e for e in self.entries if e.entry.kind == "todo"]

    @property
    def done(self) -> list[ResolvedEntry]:
        return [e for e in self.todos if e.completed]

    @property
    def open(self) -> list[ResolvedEntry]:
        return [e for e in self.todos if not e.completed]


def resolve(
    result: parser.ParseResult,
    note_id: str,
    reminders: Reminders | None = None,
) -> Resolved:
    """
    把当天页的条目与提醒事项的完成状态合并。

    提醒事项不可用时**不报错**，退化为只用备忘录的标记 ——
    日报本身仍有价值，不该因为一个来源不可用就整个失败。
    这种情况下 `reminders_available=False`，调用方应如实标注。
    """
    by_key: dict[str, Reminder] = {}
    available = False
    err: str | None = None

    try:
        rem = reminders or Reminders()
        by_key = {k: r for r in rem.all_reminders() if (k := key_of(r))}
        available = True
    except RemindersError as e:
        err = str(e)

    out: list[ResolvedEntry] = []
    not_pushed: list[parser.Entry] = []

    for entry in result.entries:
        if entry.kind != "todo":
            out.append(ResolvedEntry(entry, entry.completed is True, "notes"))
            continue

        k = make_key(note_id, entry.line_no, entry.norm)
        r = by_key.get(k)

        from_notes = entry.completed is True
        if r is None:
            if available:
                not_pushed.append(entry)
            out.append(ResolvedEntry(entry, from_notes, "notes" if from_notes else "none"))
            continue

        # 倾向"完成"：任一处为真即完成
        if r.completed or from_notes:
            src = "reminders" if r.completed else "notes"
            out.append(ResolvedEntry(entry, True, src))
        else:
            out.append(ResolvedEntry(entry, False, "reminders"))

    return Resolved(entries=out, reminders_available=available,
                    reminders_error=err, not_pushed=not_pushed)


def write_back(result: parser.ParseResult, resolved: Resolved) -> int:
    """
    把解析出的完成状态回填进留档 JSON 的条目字段。

    为什么回填：留档是后续步骤（顺延、周报）的输入，而那些步骤不该各自
    再去查一次提醒事项 —— 一次解析、一处落盘，减少对同一事实的重复判断。
    返回被改动的条目数。
    """
    by_line = {r.entry.line_no: r for r in resolved.entries}
    changed = 0
    for e in result.entries:
        r = by_line.get(e.line_no)
        if r is None or e.kind != "todo":
            continue
        new_val = r.completed
        if e.completed != new_val:
            e.completed = new_val
            changed += 1
    return changed
