#!/bin/bash
# 排查：probe_checklist 创建失败，到底有没有留下笔记？
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
FID=$(/usr/bin/python3 -c "import json;print(json.load(open('${ROOT}/config.json'))['folder_id'])")
run() { osascript -e "$1" 2>&1; }

echo "logs 文件夹当前全部条目（标题逐字打印，含长度）"
echo "════════════════════════════════════════"
run "tell application \"Notes\"
  set out to \"\"
  repeat with n in notes of folder id \"${FID}\"
    set nm to name of n
    set out to out & \"  [\" & nm & \"]  长度=\" & (length of nm) & \"  id=\" & (id of n) & linefeed
  end repeat
  if out is \"\" then return \"  （空）\"
  return out
end tell"
