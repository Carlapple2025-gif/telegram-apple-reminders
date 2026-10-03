#!/usr/bin/env python3
"""
清理 PDCA 列表里的重复条目。

背景：去重键曾经用**原样文字**，于是同一条待办在顺延后文字变了
（`勘察表盖章` → `勘察表盖章 ⟳`），被当成新内容又建了一遍。
结果：同一个待办在提醒事项里出现两份（旧的已完成、新的未完成）。

键已修（改用内容指纹，跨天/带标记都映射到同一条）。本脚本清理已经产生的重复。

判据（都要满足才删）：
  1. 条目标题里含顺延标记 ⟳（说明它是携带来的形态）
  2. 去掉标记后的内容，在列表里**另有**一条同名但无标记的条目
     —— 即"它是某条的重复"，而不是唯一的那条

安全：
  · 默认只列出，--apply 才真删
  · 逐条打印删除理由
  · 删完复核
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reminders import Reminders, RemindersError  # noqa: E402
import parse as parser  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 PDCA 列表里的重复条目")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列出）")
    args = ap.parse_args()

    try:
        rem = Reminders()
        count = rem.verify_list()
    except RemindersError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    items = rem.all_reminders()
    print(f"提醒事项列表「{rem.list_name}」共 {count} 条")
    print("═" * 56)
    print()

    # 建立"无标记内容"的索引
    plain: dict[str, list] = {}
    for r in items:
        fp = parser.content_fingerprint(r.name)
        has_mark = parser.CARRY_MARK in r.name
        plain.setdefault(fp, []).append((r, has_mark))

    plan = []
    for r in items:
        fp = parser.content_fingerprint(r.name)
        has_mark = parser.CARRY_MARK in r.name
        if not has_mark:
            continue
        # 同指纹里若存在"无标记"的条目 → 本条目是重复
        siblings = plain.get(fp, [])
        if any(not hm for _, hm in siblings):
            plan.append((r, fp))

    print("【将删除】带 ⟳ 标记、且同内容另有一条无标记的（即重复）")
    if plan:
        for r, fp in plan:
            mark = "☑" if r.completed else "☐"
            print(f"  {mark} {r.name}")
            print(f"      依据：同内容另有「{fp}」，本条是顺延形态的重复")
    else:
        print("  （无）")

    print()
    print("【保留】其余全部")
    keep_ids = {r.id for r, _ in plan}
    for r in items:
        if r.id in keep_ids:
            continue
        mark = "☑" if r.completed else "☐"
        has = " ⟳" if parser.CARRY_MARK in r.name else ""
        print(f"  {mark} {r.name}{has}")

    if not plan:
        print()
        print("✅ 无需清理。")
        return 0

    if not args.apply:
        print()
        print("═" * 56)
        print(f"【干跑】以上 {len(plan)} 条**尚未删除**。加 --apply 执行。")
        return 0

    print()
    print("【执行删除】")
    ok = 0
    for r, _ in plan:
        try:
            if rem.delete(r.id):
                print(f"  ✅ 已删除 {r.name}")
                ok += 1
            else:
                print(f"  ⚠️ 删除命令执行了，但复查时条目仍在：{r.name}")
        except RemindersError as e:
            print(f"  ❌ 删除失败 {r.name}：{e}", file=sys.stderr)

    print()
    after = rem.all_reminders()
    print(f"结果：{count} 条 → {len(after)} 条（删除 {ok} 条）")
    print()
    print("剩余条目：")
    for r in after:
        mark = "☑" if r.completed else "☐"
        print(f"  {mark} {r.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
