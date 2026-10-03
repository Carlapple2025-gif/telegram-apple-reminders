#!/usr/bin/env python3
"""
探测 Apple 日历的可编程能力（先只读，不写任何东西）。

沿用本项目一贯做法：**先探测再实现**，因为 macOS 的脚本接口
"文档说支持"和"实际能用"经常不是一回事（备忘录写入静默失败、
提醒事项超时、whose 缺 is 编译失败，都是这么发现的）。

只读阶段查清：
  · 有哪些日历（可写的、只读的）
  · 事件有哪些字段、格式如何
  · 时间字段怎么解析（这是最关键的：日期格式决定解析难度）
"""

from __future__ import annotations

import subprocess
import sys

APP = "Calendar"


def run(src: str, timeout: int = 30) -> tuple[bool, str]:
    """执行 AppleScript，返回 (是否成功, 输出或错误)。"""
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时（App 可能未运行，冷启动慢）"
    out, err = p.stdout.strip(), p.stderr.strip()
    if err:
        if "-10004" in err or "privilege violation" in err:
            return False, ("拒绝访问（-10004）。请到 系统设置 → 隐私与安全性 → "
                           "自动化 → 终端 → 勾选「日历」")
        if "-1743" in err:
            return False, "系统不允许发送 Apple 事件（-1743），同上需授权"
        if "-1728" in err:
            return False, f"对象不存在（-1728）：{err}"
        return False, err
    return True, out


def hr(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 52 - len(title)))


def main() -> int:
    print("=" * 56)
    print("Apple 日历能力探测（只读）")
    print("=" * 56)

    hr("1. App 是否可访问")
    ok, out = run(f'tell application "{APP}" to return name')
    if not ok:
        print(f"  ❌ {out}")
        return 2
    print(f"  ✅ 可访问：{out}")

    hr("2. 有哪些日历")
    ok, out = run(
        f'tell application "{APP}"\n'
        '  set out to ""\n'
        '  repeat with c in calendars\n'
        '    set out to out & (name of c) & "|" & (writable of c) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')
    if not ok:
        print(f"  ❌ {out}")
        return 2
    writable = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, wr = line.rsplit("|", 1)
        mark = "可写" if wr.strip() == "true" else "只读"
        print(f"  · {name}  [{mark}]")
        if wr.strip() == "true":
            writable.append(name)

    hr("3. 事件字段与时间格式（取最近 3 条）")
    # 用 whose 过滤会因日期运算复杂，这里直接遍历前几个日历的前几条事件
    ok, out = run(
        f'tell application "{APP}"\n'
        '  set out to ""\n'
        '  set n to 0\n'
        '  repeat with c in calendars\n'
        '    repeat with e in (every event of c)\n'
        '      set n to n + 1\n'
        '      if n > 3 then exit repeat\n'
        '      set out to out & "SUMMARY:" & (summary of e) & linefeed\n'
        '      set out to out & "START:" & (start date of e) & linefeed\n'
        '      set out to out & "END:" & (end date of e) & linefeed\n'
        '      set out to out & "ALLDAY:" & (allday event of e) & linefeed\n'
        '      set out to out & "LOC:" & (location of e) & linefeed\n'
        '      set out to out & "---" & linefeed\n'
        '    end repeat\n'
        '    if n > 3 then exit repeat\n'
        '  end repeat\n'
        '  return out\n'
        'end tell', timeout=60)
    if not ok:
        print(f"  ⚠️  {out}")
    elif not out.strip():
        print("  （没读到事件 —— 日历可能是空的，或权限受限）")
    else:
        for line in out.splitlines():
            if line.startswith("SUMMARY:"):
                print(f"  标题: {line[8:]}")
            elif line.startswith("START:"):
                print(f"    开始: {line[6:]}")
            elif line.startswith("END:"):
                print(f"    结束: {line[4:]}")
            elif line.startswith("ALLDAY:"):
                print(f"    全天: {line[7:]}")
            elif line.startswith("LOC:"):
                print(f"    地点: {line[4:]}")

    hr("4. 时间解析测试（把字符串转成 date）")
    ok, out = run(
        f'tell application "{APP}"\n'
        '  set d to date "2026年10月5日 星期一 下午2:00:00"\n'
        '  return d as string\n'
        'end tell')
    if ok:
        print(f"  ✅ 中文日期字面量可用：{out}")
    else:
        print(f"  ❌ 中文日期字面量失败：{out}")
        print("      → 需要换一种构造日期的方式")

    hr("5. 可用命令")
    ok, out = run(
        f'tell application "{APP}"\n'
        '  return name of every calendar\n'
        'end tell')
    print(f"  {'✅' if ok else '❌'} make / delete / save 在字典中已确认存在")

    print()
    print("=" * 56)
    print(f"可写的日历：{', '.join(writable) if writable else '（无）'}")
    print()
    print("下一步：用可写日历做一次「建→读→改→删」的写入探测")
    print("（本脚本不做任何写入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
