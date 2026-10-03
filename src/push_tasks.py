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
把当天页的待办同步到提醒事项。

为什么要有这一步：
    备忘录里的清单勾选框**无法用代码创建**（实测写入时会剥掉 class/data 属性），
    而"打钩"这件事在提醒事项里是最省事的 —— 锁屏小组件点一下即可，还有到期通知。
    所以：**备忘录用来看与记，提醒事项用来做与勾。**

幂等性（这一步最关键的要求）：
    这个任务每天都会跑。判重不准 → 提醒事项越积越多 → 整件事失去意义。
    做法：去重键 = note_id + 行号 + 归一化正文（见 reminders.make_key），
    存在条目的 body 里。同一天的同一行永远映射到同一条待办。

**绝不回退完成状态**：已经打钩的条目，即使备忘录里还是 `- [ ]`，
也不会被改回未完成。理由：完成状态以提醒事项为唯一权威，
而"回退"会把你亲手打过的勾抹掉。

用法：
    python3 src/push_tasks.py            # 干跑，只打印计划
    python3 src/push_tasks.py --apply    # 真正同步
    python3 src/push_tasks.py --refresh  # 同步前先从备忘录重读当天页
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import NotesError  # noqa: E402
from reminders import (  # noqa: E402
    Reminders, RemindersError, Reminder, make_key, key_of,
)
import parse as parser  # noqa: E402
import sync  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class PushPlan:
    create: list[tuple[parser.Entry, str]]      # (条目, 去重键)
    complete: list[tuple[Reminder, str]]        # (现有条目, 说明)
    already_done: list[Reminder]
    unchanged: list[Reminder]
    orphan: list[Reminder]                      # 提醒事项里有、当天页没有的

    @property
    def total_changes(self) -> int:
        return len(self.create) + len(self.complete)


def build_plan(result: parser.ParseResult, existing: list[Reminder],
               note_id: str = "") -> PushPlan:
    """
    计算同步计划。**纯函数，可离线单元测试**。

    规则：
      · 当天页的每条待办 → 按去重键找提醒事项里的对应条目
          找不到 → 新建
          找到且未完成、而备忘录里已打钩 → 置为完成（把 [x] 同步过去）
          找到且已完成 → 不动（绝不回退）
      · 提醒事项里有、但当天页已经没有的 → 只报告，不自动删
        （可能是你手动加的，也可能是备忘录里删掉了；删不删由你决定）
    """
    by_key: dict[str, Reminder] = {}
    for r in existing:
        k = key_of(r)
        if k:
            by_key[k] = r

    plan = PushPlan(create=[], complete=[], already_done=[], unchanged=[], orphan=[])

    used_keys: set[str] = set()
    for entry in result.todos:
        k = make_key(entry.norm, note_id)
        used_keys.add(k)
        current = by_key.get(k)

        if current is None:
            plan.create.append((entry, k))
            continue

        if current.completed:
            plan.already_done.append(current)
        elif entry.completed is True:
            # 备忘录里打了钩，提醒事项里还没 → 同步过去
            plan.complete.append((current, f"备忘录里已打钩：{entry.text}"))
        else:
            plan.unchanged.append(current)

    for r in existing:
        k = key_of(r)
        if k is None or k not in used_keys:
            plan.orphan.append(r)

    return plan


def sync_day(date_str: str, apply: bool = False, refresh: bool = False,
             quiet: bool = False) -> int:
    """
    执行一次同步。返回退出码。

    抽成函数而不是只留在 main 里，是为了让 daily_report 能直接调用 ——
    否则日报会出现"待办还没同步到提醒事项，所以查不到完成状态"的盲区。
    """
    say = (lambda *a: None) if quiet else print

    if refresh:
        try:
            s = sync.sync_day(date_str)
        except NotesError as e:
            print(f"❌ 读取备忘录失败：{e}", file=sys.stderr)
            return 2
        if s is None:
            print(f"⚠️  {date_str} 没有当天页")
            return 1
        result, note_id = s.result, s.note_id
    else:
        result = sync.load_archive(date_str)
        if result is None:
            print(f"没有 {date_str} 的留档", file=sys.stderr)
            return 1
        note_id = sync.archive_meta(date_str).get("note_id", "")

    try:
        rem = Reminders()
        rem.ensure_list()
        existing = rem.all_reminders()
    except RemindersError as e:
        print(f"❌ 提醒事项访问失败：{e}", file=sys.stderr)
        return 2

    plan = build_plan(result, existing, note_id)

    if plan.total_changes == 0:
        say(f"✅ 无需改动（幂等）")
        return 0

    if not apply:
        say(f"【干跑】新建 {len(plan.create)}，置完成 {len(plan.complete)} —— 未执行")
        return 0

    ok_c = ok_m = 0
    for entry, k in plan.create:
        try:
            rem.create(name=entry.text, body=k)
            ok_c += 1
        except RemindersError as e:
            print(f"  ❌ 新建失败「{entry.text}」：{e}", file=sys.stderr)
    for r, _ in plan.complete:
        try:
            rem.set_completed(r.id, True)
            ok_m += 1
        except RemindersError as e:
            print(f"  ❌ 置完成失败「{r.name}」：{e}", file=sys.stderr)

    say(f"同步完成：新建 {ok_c}/{len(plan.create)}，置完成 {ok_m}/{len(plan.complete)}")
    return 0 if (ok_c == len(plan.create) and ok_m == len(plan.complete)) else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="把当天页的待办同步到提醒事项")
    ap.add_argument("date", nargs="?", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--apply", action="store_true", help="真正同步（默认只干跑）")
    ap.add_argument("--refresh", action="store_true", help="先从备忘录重读当天页")
    args = ap.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{date_str}", file=sys.stderr)
        return 1

    # ── 取当天页
    if args.refresh:
        try:
            s = sync.sync_day(date_str)
        except NotesError as e:
            print(f"❌ 读取备忘录失败：{e}", file=sys.stderr)
            return 2
        if s is None:
            print(f"⚠️  {date_str} 没有当天页")
            return 1
        result = s.result
        note_id = s.note_id
    else:
        result = sync.load_archive(date_str)
        if result is None:
            print(f"没有 {date_str} 的留档。加 --refresh 从备忘录读取，"
                  f"或先跑 src/read_day.py {date_str}", file=sys.stderr)
            return 1
        note_id = sync.archive_meta(date_str).get("note_id", "")

    print(f"同步 {date_str} 的待办 → 提醒事项")
    print("═" * 56)
    print(f"当天页：待办 {len(result.todos)} 条，备忘 {len(result.notes)} 条")

    # ── 取提醒事项
    try:
        rem = Reminders()
        created_list = rem.ensure_list()
        if created_list:
            print(f"（已创建列表「{rem.list_name}」）")
        existing = rem.all_reminders()
    except RemindersError as e:
        print(f"❌ 提醒事项访问失败：{e}", file=sys.stderr)
        return 2

    print(f"提醒事项：列表「{rem.list_name}」现有 {len(existing)} 条")

    # ── 计划
    plan = build_plan(result, existing, note_id)

    print()
    print("── 计划 " + "─" * 48)
    if plan.create:
        print(f"  新建 {len(plan.create)} 条：")
        for entry, _ in plan.create:
            print(f"    + {entry.text}")
    if plan.complete:
        print(f"  置为完成 {len(plan.complete)} 条（备忘录里已打钩）：")
        for r, why in plan.complete:
            print(f"    ✓ {r.name}")
    if plan.already_done:
        print(f"  已完成、不动 {len(plan.already_done)} 条")
    if plan.unchanged:
        print(f"  已存在、不动 {len(plan.unchanged)} 条")
    if plan.orphan:
        print(f"  ⚠️ 提醒事项里有 {len(plan.orphan)} 条在当天页找不到（只报告，不自动删）：")
        for r in plan.orphan:
            mark = "☑" if r.completed else "☐"
            print(f"    {mark} {r.name}")

    if plan.total_changes == 0:
        print()
        print("✅ 无需改动（幂等：重复运行不会产生重复条目）")
        return 0

    if not args.apply:
        print()
        print("═" * 56)
        print("【干跑】以上改动**尚未执行**。加 --apply 真正同步。")
        return 0

    # ── 执行
    print()
    print("── 执行 " + "─" * 48)
    ok_c = ok_m = 0
    for entry, k in plan.create:
        try:
            r = rem.create(name=entry.text, body=k)
            print(f"  ✅ 新建 {r.name}")
            ok_c += 1
        except RemindersError as e:
            print(f"  ❌ 新建失败「{entry.text}」：{e}", file=sys.stderr)

    for r, _ in plan.complete:
        try:
            rem.set_completed(r.id, True)
            print(f"  ✓ 已置完成 {r.name}")
            ok_m += 1
        except RemindersError as e:
            print(f"  ❌ 置完成失败「{r.name}」：{e}", file=sys.stderr)

    print()
    print(f"完成：新建 {ok_c}/{len(plan.create)}，置完成 {ok_m}/{len(plan.complete)}")
    return 0 if (ok_c == len(plan.create) and ok_m == len(plan.complete)) else 3


if __name__ == "__main__":
    sys.exit(main())
