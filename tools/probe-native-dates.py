#!/usr/bin/env python3
"""
探测：日报那两件事能不能**用 Apple 原生条件**实现（全部只读）。

要回答两个具体问题：

① 「今日完成」能不能让**提醒事项自己**筛？
   目前 `all_reminders()` 读全量、只取 completed 布尔值，
   于是昨天、上周完成的条目全都落在"今日完成"里。
   候选原生能力：`completion date` + `whose ... is greater than`
   （服务端过滤，不用把全量拉回来）。

② 「备忘删掉就不再提醒」能不能让**备忘录自己**告诉我？
   候选原生能力：`modification date`（版本比对）。
   若可行，就不需要 agent 维护任何差集状态。

沿用本项目做法：**先探测再实现** —— macOS 的脚本接口
"文档说支持"和"实际能用"经常不是一回事
（备忘录静默失败、提醒事项超时、whose 缺 is 编译失败，都是这么发现的）。

只读，不写任何东西。
"""

from __future__ import annotations

import subprocess
import sys

ROOT = None


def run(src: str, timeout: int = 45) -> tuple[bool, str]:
    """跑一段 AppleScript，返回 (成功, 输出或错误)。"""
    try:
        p = subprocess.run(["osascript", "-e", src], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时（-1712 类：App 可能没在运行，首次调用要冷启动）"
    if p.returncode != 0:
        return False, (p.stderr or "").strip().replace("\n", " ")[:200]
    return True, (p.stdout or "").strip()


def show(label: str, ok: bool, out: str) -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}")
    for line in out.splitlines()[:6]:
        print(f"       {line}")


def probe_reminders() -> None:
    print("① 提醒事项：能不能用原生条件筛「今日已完成」？")
    print()

    # 1a. completion date 是否可读
    ok, out = run(
        'tell application "Reminders"\n'
        '  set L to first list\n'
        '  set out to ""\n'
        '  repeat with r in (every reminder of L whose completed is true)\n'
        '    set out to out & (name of r) & " ||| " & '
        '(completion date of r as string) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    show("completed 的条目能读出 completion date", ok, out)
    print()

    # 1b. whose 用 completion date 做比较（服务端过滤）
    ok, out = run(
        'set d to (current date) - 1 * days\n'
        'tell application "Reminders"\n'
        '  set L to first list\n'
        '  set out to ""\n'
        '  repeat with r in (every reminder of L whose completion date '
        'is greater than d)\n'
        '    set out to out & (name of r) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    show("whose completion date is greater than <昨天>（原生服务端过滤）",
         ok, out)
    print()

    # 1c. 缺 is 的写法是否也能过（项目里踩过 whose 缺 is 编译失败）
    ok, out = run(
        'set d to (current date) - 1 * days\n'
        'tell application "Reminders"\n'
        '  set L to first list\n'
        '  return (count of (every reminder of L whose completion date > d)) '
        'as string\n'
        'end tell'
    )
    show("简写 whose completion date > d 是否可用（缺 is）", ok, out)
    print()

    # 1d. 未完成条目是否也带 completion date（应为 missing value）
    ok, out = run(
        'tell application "Reminders"\n'
        '  set L to first list\n'
        '  set r to first reminder of L whose completed is false\n'
        '  return (completion date of r as string)\n'
        'end tell'
    )
    show("未完成条目的 completion date（预期 missing value）", ok, out)
    print()


def probe_notes() -> None:
    print("② 备忘录：能不能用原生条件判断「这条被删了」？")
    print()

    # 2a. 笔记有没有 modification date / creation date
    ok, out = run(
        'tell application "Notes"\n'
        '  set n to first note\n'
        '  return "modification: " & (modification date of n as string) & '
        'linefeed & "creation: " & (creation date of n as string)\n'
        'end tell'
    )
    show("note 有 modification date / creation date", ok, out)
    print()

    # 2b. 那条 note 的全部属性（看有没有可用的删除/状态线索）
    ok, out = run(
        'tell application "Notes"\n'
        '  return (properties of first note) as string\n'
        'end tell'
    )
    show("note 的全部 properties（找有没有删除线索）", ok, out)
    print()

    # 2c. 有没有"最近删除"文件夹能被识别（僵尸笔记的坑）
    ok, out = run(
        'tell application "Notes"\n'
        '  set out to ""\n'
        '  repeat with f in every folder\n'
        '    set out to out & (name of f) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell'
    )
    show("文件夹列表（看有没有 Recently Deleted 之类）", ok, out)
    print()

    # 2d. 能不能按日期筛笔记（若可行则不需要全量拉）
    ok, out = run(
        'set d to (current date) - 7 * days\n'
        'tell application "Notes"\n'
        '  return (count of (every note whose modification date is greater '
        'than d)) as string\n'
        'end tell'
    )
    show("whose modification date is greater than <7天前>", ok, out)
    print()


def main() -> int:
    print("日报两件事：能不能用 Apple 原生条件实现")
    print("═" * 46)
    print("（全部只读，不写任何东西）")
    print()
    probe_reminders()
    probe_notes()
    print("═" * 46)
    print("说明：❌ 不代表做不到，只代表这条路径不可用；")
    print("      换写法再试，或改用别的原生命令。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
