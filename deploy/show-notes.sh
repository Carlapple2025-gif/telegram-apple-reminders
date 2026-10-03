#!/bin/bash
# 打印 logs 文件夹里所有条目的内容（只读，排查用）
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
FID=$(/usr/bin/python3 -c "import json;print(json.load(open('${ROOT}/config.json'))['folder_id'])")
run() { osascript -e "$1" 2>&1; }

run "tell application \"Notes\"
  set out to \"\"
  repeat with n in notes of folder id \"${FID}\"
    set out to out & \"════ \" & (name of n) & \" ════\" & linefeed
    set out to out & (plaintext of n) & linefeed & linefeed
  end repeat
  return out
end tell"
