#!/usr/bin/env python3
"""
探测：提醒事项的可编程能力。

背景：备忘录的清单勾选框无法用代码创建（实测备忘录写入时会剥掉
class/data 属性，清单状态根本不存储在 HTML 里）。所以考虑把「待办」
交给提醒事项 —— 它的脚本字典里有 completed / completion date / due date
等完整属性。本脚本把关键能力一次验掉。

为什么用独立测试列表：
    全程只操作名为 PDCA-TEST-<pid> 的**新建列表**，跑完删掉该列表。
    完全不碰你已有的待办列表与条目。

要验证的六件事（每一件都对应设计里的一个依赖）：
  1. 能否新建列表                     —— 隔离测试环境
  2. 能否新建提醒（含 due date）       —— 生成待办
  3. 能否读回提醒（按 id 定位）        —— 后续要按 id 操作
  4. 能否设置 completed               —— 同步完成状态（核心）
  5. 能否读到 completed 的变化         —— 反向同步（你在别处打钩）
  6. 能否删除提醒与列表               —— 幂等与清理
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RemindersError(RuntimeError):
    pass


def lit(s: str) -> str:
    """转成 AppleScript 字符串字面量。反斜杠必须最先转义。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run(source: str) -> str:
    proc = subprocess.run(["osascript", "-e", source], capture_output=True, text=True)
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    if err:
        if "-10004" in err or "privilege violation" in err:
            raise RemindersError(
                "提醒事项拒绝访问（-10004 越权）。\n"
                "请到 系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「提醒事项」"
            )
        if "-1743" in err:
            raise RemindersError(
                "系统不允许向「提醒事项」发送 Apple 事件（-1743）。同上需授权。"
            )
        raise RemindersError(f"AppleScript 失败：{err}")
    return out


results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"   {'✅' if ok else '❌'} {name}" + (f"  —— {detail}" if detail else ""))


def main() -> int:
    list_name = f"PDCA-TEST-{os.getpid()}"

    print("提醒事项能力探测")
    print("═" * 56)
    print(f"使用独立测试列表：「{list_name}」（跑完删除）")
    print()

    # ── 0. 基本访问
    print("── 0. 基本访问 ──")
    try:
        existing = run('tell application "Reminders" to get name of every list')
        record("可访问提醒事项", True)
        print(f"      现有列表：{existing or '（无）'}")
    except RemindersError as e:
        print(f"   ❌ {e}")
        return 2

    # ── 1. 新建列表
    print()
    print("── 1. 新建列表 ──")
    try:
        run(
            'tell application "Reminders"\n'
            f'  make new list with properties {{name:{lit(list_name)}}}\n'
            '  return "ok"\n'
            'end tell'
        )
        names = run('tell application "Reminders" to get name of every list')
        ok = list_name in names
        record("新建列表", ok, "" if ok else f"列表里没有 {list_name}")
        if not ok:
            return 3
    except RemindersError as e:
        record("新建列表", False, str(e))
        return 3

    # ── 2. 新建提醒（含 due date）
    print()
    print("── 2. 新建提醒（含到期时间）──")
    marker = f"探测条目-{os.getpid()}"
    try:
        # due date 用 AppleScript 的 date 构造：current date 加偏移
        run(
            'tell application "Reminders"\n'
            f'  set theList to list {lit(list_name)}\n'
            f'  make new reminder at theList with properties '
            f'{{name:{lit(marker)}, body:"探测用备注", due date:(current date) + 1 * hours}}\n'
            '  return "ok"\n'
            'end tell'
        )
        record("新建提醒", True)
    except RemindersError as e:
        record("新建提醒", False, str(e))

    # ── 3. 读回（按 id 定位）
    print()
    print("── 3. 读回并定位 ──")
    rid = ""
    try:
        out = run(
            'tell application "Reminders"\n'
            f'  set theList to list {lit(list_name)}\n'
            f'  repeat with r in (every reminder of theList whose name is {lit(marker)})\n'
            '    return (id of r)\n'
            '  end repeat\n'
            '  return "NOTFOUND"\n'
            'end tell'
        )
        if out == "NOTFOUND":
            record("按名字找到提醒", False, "没找到刚建的条目")
        else:
            rid = out
            record("按名字找到提醒", True, f"id={rid[:26]}…")
    except RemindersError as e:
        record("按名字找到提醒", False, str(e))

    if rid:
        try:
            out = run(
                'tell application "Reminders"\n'
                f'  set hits to (every reminder whose id is {lit(rid)})\n'
                '  if (count of hits) is 0 then return "NOTFOUND"\n'
                '  set r to item 1 of hits\n'
                '  return (name of r) & "|" & (completed of r as string) & "|" & (body of r)\n'
                'end tell'
            )
            if out == "NOTFOUND":
                record("按 id 定位提醒", False)
            else:
                parts = out.split("|")
                record("按 id 定位提醒", True, f"name={parts[0][:20]}… completed={parts[1]}")
        except RemindersError as e:
            record("按 id 定位提醒", False, str(e))

    # ── 4. 设置 completed（核心能力）
    print()
    print("── 4. 设置完成状态（核心）──")
    if rid:
        try:
            run(
                'tell application "Reminders"\n'
                f'  set hits to (every reminder whose id is {lit(rid)})\n'
                '  if (count of hits) is 0 then return "NOTFOUND"\n'
                '  set completed of item 1 of hits to true\n'
                '  return "ok"\n'
                'end tell'
            )
            # 读回验证
            check = run(
                'tell application "Reminders"\n'
                f'  set hits to (every reminder whose id is {lit(rid)})\n'
                '  if (count of hits) is 0 then return "NOTFOUND"\n'
                '  return (completed of item 1 of hits as string)\n'
                'end tell'
            )
            ok = check.strip().lower() == "true"
            record("设置 completed=true 并读回验证", ok, f"读回 {check}")
        except RemindersError as e:
            record("设置 completed=true 并读回验证", False, str(e))

    # ── 5. 反向读取（用 completed 过滤）
    print()
    print("── 5. 按完成状态过滤（反向同步用）──")
    try:
        done = run(
            'tell application "Reminders"\n'
            f'  set theList to list {lit(list_name)}\n'
            '  return (count of (every reminder of theList whose completed is true)) as string\n'
            'end tell'
        )
        openc = run(
            'tell application "Reminders"\n'
            f'  set theList to list {lit(list_name)}\n'
            '  return (count of (every reminder of theList whose completed is false)) as string\n'
            'end tell'
        )
        record("可按 completed 过滤", True, f"已完成 {done} 条 / 未完成 {openc} 条")
    except RemindersError as e:
        record("可按 completed 过滤", False, str(e))

    # ── 6. 清理
    print()
    print("── 6. 清理测试列表 ──")
    try:
        run(
            'tell application "Reminders"\n'
            f'  delete list {lit(list_name)}\n'
            '  return "ok"\n'
            'end tell'
        )
        names = run('tell application "Reminders" to get name of every list')
        ok = list_name not in names
        record("删除测试列表", ok, "" if ok else "列表仍在，请手动删除")
    except RemindersError as e:
        record("删除测试列表", False, f"{e}（请手动删除 {list_name}）")

    # ── 结论
    print()
    print("═" * 56)
    print("结论")
    print("═" * 56)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print()
    print(f"通过 {passed}/{len(results)} 项")

    core = {n: ok for n, ok, _ in results}
    can_create = core.get("新建提醒", False)
    can_complete = core.get("设置 completed=true 并读回验证", False)
    can_filter = core.get("可按 completed 过滤", False)

    print()
    if can_create and can_complete and can_filter:
        print("  → 提醒事项完全可用：能生成待办、能设完成状态、能反向读取。")
        print("    把待办交给提醒事项这条路可行，且比备忘录更有能力。")
    elif can_create and can_complete:
        print("  → 能生成与完成，但按状态过滤受限 —— 反向同步需换实现方式。")
    elif can_create:
        print("  → 只能生成，不能改完成状态。同步会是单向的。")
    else:
        print("  → 提醒事项不可用，需重新考虑方案。")
    print()
    return 0 if (can_create and can_complete) else 1


if __name__ == "__main__":
    sys.exit(main())
