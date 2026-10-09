#!/usr/bin/env python3
"""
探针：提醒事项的「完成历史」能不能读、读多快、筛不筛得动。

为什么需要它（2026-10-10）：
  设计里最重要的一处改动是「习惯从日历搬进提醒事项的重复条目」——
  因为**只有它同时有"重复"和"完成态"**。但这个设计成立的前提是
  平台层真的能读到"哪一天完成了"：

    ① 已完成条目会不会留下？留下时有没有 completion date？
    ② AppleScript 能不能按 completed 原生筛（而不是全表读回 Python 筛）？
       —— 全表扫描是这份读取里唯一会随条目数变差的地方（日历那边踩过）。
    ③ 一条重复提醒完成后，是"生成新的一条"还是"覆盖原来那条"？
       —— 这决定周完成率能不能算。（本探针只能回答 ① ②；
       ③ 需要你先手工建一条重复提醒，几天后再跑一次。）

**只读**：不创建、不修改、不删除任何东西。
用法：  python3 tools/probe-reminders-history.py [列表名]
退出码：0 = 读到；1 = 权限或平台问题（会把原始报错打出来）
"""
import json
import subprocess
import sys
import time


def run(src: str, timeout: int = 120) -> tuple[str, str, int]:
    t0 = time.time()
    p = subprocess.run(["/usr/bin/osascript", "-e", src],
                       capture_output=True, text=True, timeout=timeout)
    return p.stdout.strip(), p.stderr.strip(), int((time.time() - t0) * 1000)


LIST_ARG = sys.argv[1] if len(sys.argv) > 1 else ""

# 用 JSON 回传：字段里有全角/换行也不会被拆错
SETUP = ('tell application "Reminders"\n'
         + (f'  set theList to list "{LIST_ARG}"\n' if LIST_ARG
            else '  set theList to default list\n')
         + '  set listName to name of theList\n')

print("=" * 62)
print("探针：提醒事项的完成历史（只读）")
print("=" * 62)

# ── ① 计数：总数 / 未完成 / 已完成（用原生条件筛）
src_count = SETUP + (
    '  set nAll to count of reminders in theList\n'
    '  set nOpen to count of (reminders in theList whose completed is false)\n'
    '  set nDone to count of (reminders in theList whose completed is true)\n'
    '  return listName & tab & nAll & tab & nOpen & tab & nDone\n'
    'end tell')
out, err, ms = run(src_count)
print(f"\n① 计数（原生 whose 筛选）—— {ms} ms")
if err or not out:
    print("   ❌ 读不到：", err[:400] or "（空输出）")
    print("\n   → 这多半是 TCC 授权问题（-1743 未授权 / -10004 越权）。")
    print("     授权挂在**调用进程**上：在终端里跑就能读到（README §七）。")
    sys.exit(1)
name, n_all, n_open, n_done = (out.split("\t") + ["", "", "", ""])[:4]
print(f"   列表 = {name!r}")
print(f"   总数 {n_all} ｜ 未完成 {n_open} ｜ 已完成 {n_done}")

# ── ② 已完成条目：有没有 completion date（拿最多 8 条样本）
src_done = SETUP + (
    '  set out to ""\n'
    '  set k to 0\n'
    '  repeat with r in (reminders in theList whose completed is true)\n'
    '    set k to k + 1\n'
    '    if k > 8 then exit repeat\n'
    '    set cd to ""\n'
    '    try\n'
    '      set cd to (completion date of r) as string\n'
    '    end try\n'
    '    set md to ""\n'
    '    try\n'
    '      set md to (modification date of r) as string\n'
    '    end try\n'
    '    set out to out & (name of r) & tab & cd & tab & md & linefeed\n'
    '  end repeat\n'
    '  return out\n'
    'end tell')
out2, err2, ms2 = run(src_done)
print(f"\n② 已完成条目样本（最多 8 条）—— {ms2} ms")
if err2:
    print("   ❌ ", err2[:300])
else:
    rows = [l for l in out2.splitlines() if l.strip()]
    if not rows:
        print("   （列表里还没有已完成条目 —— 先去 App 里打钩一条再跑）")
    for l in rows:
        parts = l.split("\t")
        nm = parts[0][:38]
        cd = parts[1] if len(parts) > 1 and parts[1] else "（空 ✗）"
        md = parts[2] if len(parts) > 2 and parts[2] else "—"
        print(f"   · {nm:<40} 完成时间 = {cd:<28} 修改时间 = {md}")

# ── ③ 全量读回 Python 筛 vs 原生筛：比一下代价
src_all = SETUP + (
    '  set out to ""\n'
    '  repeat with r in (reminders in theList)\n'
    '    set c to "0"\n'
    '    if completed of r then set c to "1"\n'
    '    set out to out & (name of r) & tab & c & linefeed\n'
    '  end repeat\n'
    '  return out\n'
    'end tell')
out3, err3, ms3 = run(src_all)
print(f"\n③ 全量读（回 Python 筛）—— {ms3} ms")
if err3:
    print("   ❌ ", err3[:300])
else:
    lines = [l for l in out3.splitlines() if l.strip()]
    json.dump({"list": name, "all": len(lines)}, sys.stdout)
    print(f"\n   读到 {len(lines)} 条")

print("\n" + "=" * 62)
print("结论（把这两行抄给设计文档）：")
print(f"  · 完成历史可读：{'✅' if not err2 else '❌'}（completion date 字段{'有' if not err2 else '读不到'}）")
print(f"  · 原生筛 vs 全量读：{ms} ms vs {ms3} ms"
      + ("　→ 原生筛更快，应该用它" if ms3 > ms else "　→ 两者相当"))
print("=" * 62)
