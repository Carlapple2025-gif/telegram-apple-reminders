#!/usr/bin/env python3
"""探测：备忘录里有哪些文件夹（只读）。用于确定「备忘」文件夹的名字是否可用。"""
import subprocess, sys

def run(src, timeout=30):
    p = subprocess.run(["osascript", "-e", src], capture_output=True, text=True, timeout=timeout)
    if p.stderr.strip():
        return False, p.stderr.strip()
    return True, p.stdout.strip()

ok, out = run('''tell application "Notes"
  set out to ""
  repeat with f in folders
    set out to out & (name of f) & "|" & (id of f) & "|" & (count of notes of f) & linefeed
  end repeat
  return out
end tell''')
if not ok:
    print("❌", out); sys.exit(2)
print("备忘录里的文件夹（名字 | id | 笔记数）：")
writable_names = []
for line in out.splitlines():
    if line.count("|") < 2: continue
    name, fid, n = line.rsplit("|", 2)
    print(f"  · {name}  [{n} 条]")
    writable_names.append(name)
print()
print("「备忘」已存在" if "备忘" in writable_names else "「备忘」尚不存在（需要新建）")
