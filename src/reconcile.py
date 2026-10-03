#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════════════
# ⚠️ v1 遗留代码 —— **v4 不调用本文件**
#
# v4 的唯一输入入口是 Telegram（daemon.py → intake.py → reminders/applecal/memo），
# 已没有"同步""顺延""三处状态合并"这些概念。本文件属于被推翻的 v1 方案，
# 保留原因：v1 是唯一能读写备忘录正文的代码，万一要复用不必翻 git 历史。
#
# 重新启用前请先读 docs/ARCHITECTURE.md 的「为什么推翻 v1」——
# 这批代码的共同根因是**一条状态存在三处**，于是不得不"猜身份"
# （行号 / 原样文字 / ⟳ 标记），并因此产生四次同源 bug。
# ════════════════════════════════════════════════════════════════════════
"""
调和：把「提醒事项里已完成」的条目在备忘录当天页里也打上钩。

为什么需要它：
    完成状态的权威是提醒事项，但**备忘录里那一行的 `[ ]` 不会自动变**。
    于是出现矛盾状态：
        备忘录 10-03：`- [ ] 勘察表盖章 ⟳`（看起来未完成）
        提醒事项：     同一件事已标记完成 ☑
    实测踩到过：昨晚在提醒事项打了钩，次日顺延仍把它带到新的一天
    （顺延依据的是过期快照），于是新页面里出现一条"其实已完成"的待办。

本脚本把备忘录里对应的 `- [ ]` 改成 `- [x]`，让两处一致。

⚠️ 这是**唯一会修改你备忘录原文的功能**（顺延只追加新行，不改已有行）。
理由：不加这一步，"看起来未完成"的条目会一直留在页面上误导你，
而且下次顺延时会被继续往后带。改动仅限于把 `[ ]` 变成 `[x]`，
不碰文字、不删行。

安全：
  · 默认只列出将改什么，--apply 才写入
  · 逐个替换后读回验证
  · 只在该行**当前是 `[ ]`** 且权威状态为已完成时才改
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError  # noqa: E402
import completion  # noqa: E402
import parse as parser  # noqa: E402
import sync  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser(description="让备忘录当天页与提醒事项的完成状态一致")
    ap.add_argument("date", nargs="?", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--apply", action="store_true", help="真正修改备忘录（默认只列出）")
    args = ap.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{date_str}", file=sys.stderr)
        return 1

    # 先同步一次，拿到最新状态
    try:
        s = sync.sync_day(date_str)
    except NotesError as e:
        print(f"❌ 读取备忘录失败：{e}", file=sys.stderr)
        return 2
    if s is None:
        print(f"⚠️  备忘录里没有 {date_str} 的当天页")
        return 1

    result = s.result
    resolved = completion.resolve(result, s.note_id)

    if not resolved.reminders_available:
        print(f"❌ 提醒事项不可用，无法判断权威状态：{resolved.reminders_error}",
              file=sys.stderr)
        return 2

    # 找出：权威说已完成、但备忘录里还写着 [ ]
    need_fix = [r for r in resolved.done
                if r.entry.completed is not True and r.entry.kind == "todo"]

    print(f"核对 {date_str}：")
    print(f"  待办 {len(resolved.todos)} 条 · 权威判定已完成 {len(resolved.done)} 条")
    print()

    if not need_fix:
        print("✅ 备忘录与提醒事项一致，无需改动。")
        return 0

    print("【将修改】备忘录里这些行会从 [ ] 改成 [x]：")
    for r in need_fix:
        print(f"  行{r.entry.line_no}  {r.entry.raw}")
        print(f"         权威状态：已完成（来源 {r.source}）")

    if not args.apply:
        print()
        print("（干跑：未修改备忘录。加 --apply 执行）")
        return 0

    # 执行：按行精确替换 [ ] → [x]
    note = Notes()
    cur = note.plaintext_of(s.note_id)
    if cur is None:
        print("❌ 读不到当天页正文", file=sys.stderr)
        return 3

    raw_lines = cur.replace("\r\n", "\n").split("\n")
    changed = 0
    for r in need_fix:
        idx = r.entry.line_no - 1
        if not (0 <= idx < len(raw_lines)):
            continue
        line = raw_lines[idx]
        if "[ ]" not in line:
            continue
        raw_lines[idx] = line.replace("[ ]", "[x]", 1)
        changed += 1

    if not changed:
        print("⚠️  没有实际改动（行内容与预期不符，可能页面已被编辑）")
        return 1

    # 整页重写（用 <div> 包每行 —— 实测必需，否则会被折叠成一行）
    html = "".join(f"<div>{_esc(l)}</div>" for l in raw_lines)
    try:
        from notes import run_applescript, _as_literal
        run_applescript(
            'tell application "Notes"\n'
            f'  set hits to (every note of folder id {_as_literal(note.folder_id)} '
            f'whose id is {_as_literal(s.note_id)})\n'
            '  if (count of hits) is 0 then return "NOTFOUND"\n'
            '  set body of item 1 of hits to ' + _as_literal(html) + '\n'
            '  return "ok"\n'
            'end tell'
        )
    except NotesError as e:
        print(f"❌ 写入失败：{e}", file=sys.stderr)
        return 3

    # 读回验证
    after = note.plaintext_of(s.note_id) or ""
    print()
    print(f"✅ 已修改 {changed} 行")
    print()
    print("读回验证：")
    for l in after.split("\n"):
        if l.strip():
            print(f"  {l}")
    return 0


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    sys.exit(main())
