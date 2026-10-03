#!/bin/bash
# 检查当天页的 HTML 结构（看手动勾选框变成什么样了）
#
# 目的：你手动把 `- [ ]` 转成了真正的清单项，我需要看清备忘录把它存成了
# 什么 HTML —— 这决定解析器是按纯文本解析，还是按 HTML 解析，
# 也决定代码能否自动生成同样的结构。
#
# 只读。

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FOLDER_ID=$(/usr/bin/python3 -c "import json;print(json.load(open('$ROOT/config.json'))['folder_id'])")
TARGET="${1:-2026-10-02}"

run() { osascript -e "$1" 2>&1; }

echo "当天页 HTML 结构检查：「${TARGET}」"
echo "════════════════════════════════════════"
echo

echo "── 1. name（标题）──"
run "tell application \"Notes\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    if (name of n) starts with \"$TARGET\" then return name of n
  end repeat
  return \"NOTFOUND\"
end tell"

echo
echo "── 2. plaintext（会被渲染成什么）──"
run "tell application \"Notes\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    if (name of n) starts with \"$TARGET\" then return plaintext of n
  end repeat
  return \"NOTFOUND\"
end tell"

echo
echo "── 3. body（真实 HTML，完整打印）──"
run "tell application \"Notes\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    if (name of n) starts with \"$TARGET\" then return body of n
  end repeat
  return \"NOTFOUND\"
end tell"

echo
echo "── 4. body 的关键结构特征 ──"
run "tell application \"Notes\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    if (name of n) starts with \"$TARGET\" then
      set b to body of n
      set out to \"长度: \" & (length of b) & linefeed
      if b contains \"checklist\" then
        set out to out & \"含 'checklist' 字样: 是\" & linefeed
      else
        set out to out & \"含 'checklist' 字样: 否\" & linefeed
      end if
      if b contains \"<ul\" then
        set out to out & \"含 <ul>: 是\" & linefeed
      else
        set out to out & \"含 <ul>: 否\" & linefeed
      end if
      if b contains \"<li\" then
        set out to out & \"含 <li>: 是\" & linefeed
      else
        set out to out & \"含 <li>: 否\" & linefeed
      end if
      if b contains \"[ ]\" then
        set out to out & \"纯文本 [ ]: 仍在\" & linefeed
      else
        set out to out & \"纯文本 [ ]: 已消失\" & linefeed
      end if
      return out
    end if
  end repeat
  return \"NOTFOUND\"
end tell"
