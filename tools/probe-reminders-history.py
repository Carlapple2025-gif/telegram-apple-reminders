#!/usr/bin/env python3
"""
探针：提醒事项的「完成历史」能不能读、读多快、筛不筛得动。

2026-10-10 首次实测结果（macOS 27.0.1，默认列表 7 条）：
  · 完成历史**可读**，completion date 字段确实有 ✓（最早样本到 2026-07-28）
  · 原生筛计数        1194 ms（固定开销 ~1.2 s）
  · 全量读 7 条       2150 ms（~0.3 s/条）
  · 读已完成明细 5 条 6983 ms（**~1.4 s/条** ⚠️ 代价随历史条数线性增长）
  → 硬约束：**不能每晚读全量历史**，只能读"最近 N 天"的窗口，
    更长的统计从 journal 算（见 docs/CADENCE.md §九）。

本脚本**只读**：不创建、不修改、不删除任何东西。

用法：
    python3 tools/probe-reminders-history.py [列表名]
    python3 tools/probe-reminders-history.py --inventory [列表名]
        （inventory：把每条的名字/id/到期/完成态原样打出来，
          用来隔几天对比 —— 回答"重复提醒完成后是生成新条还是覆盖原条"）
"""
import re
import subprocess
import sys
import time


def run(src: str, timeout: int = 180) -> tuple[str, str, int]:
    t0 = time.time()
    p = subprocess.run(["/usr/bin/osascript", "-e", src],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip(), p.stderr.strip(), int((time.time() - t0) * 1000)


def compiles(src: str) -> str:
    """只编译不执行 —— 用来在没有授权时也能验证 AppleScript 语法。"""
    p = subprocess.run(["/usr/bin/osacompile", "-e", src, "-o", "/tmp/_probe.scpt"],
                       capture_output=True, text=True)
    return "" if p.returncode == 0 else (p.stderr.strip()[:200] or "编译失败")


ARGS = [a for a in sys.argv[1:] if a != "--inventory"]
INVENTORY = "--inventory" in sys.argv
LIST_ARG = ARGS[0] if ARGS else ""

SETUP = ('tell application "Reminders"\n'
         + (f'  set theList to list "{LIST_ARG}"\n' if LIST_ARG
            else '  set theList to default list\n')
         + '  set listName to name of theList\n')

# ── ④ 里要用的那句（也是日报将来的读法）：按完成日期开窗口
WINDOW_DAYS = 3
WINDOW_SRC = ('  set cutoff to (current date) - ' + str(WINDOW_DAYS) + ' * days\n'
              '  set win to (reminders in theList whose completed is true '
              'and completion date ≥ cutoff)\n'
              '  set wc to count of win\n')

print("=" * 62)
print("探针：提醒事项的完成历史（只读）")
print("=" * 62)

# ── ⓪ 先只编译不执行：语法错了立刻能看出来（不需要授权）
for _name, _src in (("计数", SETUP + '  return "x"\nend tell'),
                    ("窗口筛", SETUP + WINDOW_SRC + '  return "x"\nend tell')):
    _err = compiles(_src)
    print(f"⓪ {_name} 语句编译：{'✅' if not _err else '❌ ' + _err}")

if INVENTORY:
    src_inv = SETUP + (
        '  set out to ""\n'
        '  repeat with r in (reminders in theList)\n'
        '    set cd to ""\n'
        '    try\n'
        '      if completed of r then set cd to (completion date of r) as string\n'
        '    end try\n'
        '    set dd to ""\n'
        '    try\n'
        '      if due date of r is not missing value then set dd to (due date of r) as string\n'
        '    end try\n'
        '    set rm to ""\n'
        '    try\n'
        '      if remind me date of r is not missing value then set rm to (remind me date of r) as string\n'
        '    end try\n'
        '    set cflag to "0"\n'
        '    if completed of r then set cflag to "1"\n'
        '    set out to out & (id of r) & tab & cflag & tab & dd & tab & rm & tab '
        '& cd & tab & (name of r) & linefeed\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')
    out, err, ms = run(src_inv)
    print(f"\n⑤ 清单快照（{ms} ms）—— 隔几天再跑一次，对比 id 就能回答「重复提醒的完成行为」")
    if err:
        print("   ❌", err[:300])
        sys.exit(1)
    for line in out.splitlines():
        if not line.strip():
            continue
        f = line.split("\t")
        rid = re.sub(r"^x-apple-reminder://", "", f[0])[:8] if f else "?"
        print(f"   id={rid}… 完成={f[1]}  到期={(f[2] or '—')[:26]}  "
              f"提醒={(f[3] or '—')[:26]}  {f[5][:34] if len(f) > 5 else ''}")
    print("\n   把这份快照存下来（tee 到文件），几天后再跑一次对比 ✓")
    sys.exit(0)

# ── ① 计数
out, err, ms = run(SETUP + (
    '  set nAll to count of reminders in theList\n'
    '  set nOpen to count of (reminders in theList whose completed is false)\n'
    '  set nDone to count of (reminders in theList whose completed is true)\n'
    '  return listName & tab & nAll & tab & nOpen & tab & nDone\n'
    'end tell'))
print(f"\n① 计数（原生筛）—— {ms} ms")
if err or not out:
    print("   ❌ 读不到：", err[:300] or "（空输出）")
    print("   → TCC 授权问题：授权挂在**调用进程**上，换到终端里跑（README §七）。")
    sys.exit(1)
name, n_all, n_open, n_done = (out.split("\t") + ["", "", "", ""])[:4]
print(f"   列表 = {name!r} ｜ 总数 {n_all} ｜ 未完成 {n_open} ｜ 已完成 {n_done}")

# ── ② **最近**完成的样本（只取 3 条：每条 ~1 秒，取多了纯浪费）
#    2026-10-10 改：原来取的是"最老的几条"（repeat 从头开始），参考价值低 ——
#    现在只在窗口内取，看的就是最近发生了什么。
src_done = SETUP + (
    '  set cutoff to (current date) - 14 * days\n'
    '  set out to ""\n'
    '  set k to 0\n'
    '  repeat with r in (reminders in theList whose completed is true '
    'and completion date ≥ cutoff)\n'
    '    set k to k + 1\n'
    '    if k > 3 then exit repeat\n'
    '    set cd to ""\n'
    '    try\n'
    '      set cd to (completion date of r) as string\n'
    '    end try\n'
    '    set out to out & (name of r) & tab & cd & linefeed\n'
    '  end repeat\n'
    '  return out\n'
    'end tell')
out2, err2, ms2 = run(src_done)
print(f"\n② 已完成样本（3 条）—— {ms2} ms"
      + (f"　→ ~{ms2 // 3} ms/条" if not err2 and ms2 else ""))
if err2:
    print("   ❌", err2[:300])
elif not out2.strip():
    print("   （还没有已完成条目）")
else:
    for l in out2.splitlines():
        if l.strip():
            p = l.split("\t")
            print(f"   · {p[0][:36]:<38} {p[1] if len(p) > 1 else ''}")

# ── ③ 全量读（对照）
out3, err3, ms3 = run(SETUP + (
    '  set out to ""\n'
    '  repeat with r in (reminders in theList)\n'
    '    set c to "0"\n'
    '    if completed of r then set c to "1"\n'
    '    set out to out & (name of r) & tab & c & linefeed\n'
    '  end repeat\n'
    '  return out\n'
    'end tell'))
print(f"\n③ 全量读（回 Python 筛）—— {ms3} ms")
print(f"   读到 {len([l for l in out3.splitlines() if l.strip()])} 条")

# ── ④ 按完成日期开窗口（**日报将来的读法**）
src_win = SETUP + WINDOW_SRC + '  return listName & tab & wc\nend tell'
out4, err4, ms4 = run(src_win)
print(f"\n④ 窗口筛：近 {WINDOW_DAYS} 天完成（**这是日报要用的读法**）—— {ms4} ms")
if err4:
    print("   ❌ 不行：", err4[:300])
    print("   → 退路：读全量后在 Python 里按日期筛（代价高，见 §九 的留档方案）")
else:
    print(f"   ✅ 可以：{out4.split(chr(9))[-1] if chr(9) in out4 else out4} 条落在窗口内")

print("\n" + "=" * 62)
print("结论：")
print(f"  · 完成历史可读   : {'✅' if not err2 else '❌'}（completion date 有）")
print(f"  · 逐条明细的代价 : ~{ms2 // 3 if ms2 else '?'} ms/条  ← 决定「不能读全量」")
print(f"  · 窗口筛能不能用 : {'✅ 能' if not err4 else '❌ 不能（改用留档）'}")
print("=" * 62)
