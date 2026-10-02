#!/bin/bash
# 删除指定标题的当天页（默认 2026-10-03）。
#
# 为什么需要它：`install_launchd.sh test` 会立即触发顺延任务，
# 而顺延任务按设计会新建次日页 —— 提前把明天的页面建出来了。
# 删掉它，明天 07:00 就能按当天页的**最终状态**正常承接一次。
#
# 安全设计：
#   · **精确匹配**标题（`is` 而不是 `contains`/`starts with`），
#     多一个字都不删 —— 你的真实笔记标题是「2026-10-03 ...」这类
#     长标题的风险必须排除
#   · 只在该日期页确实存在时才删，删完复核，并报出剩余条数
#   · 只操作配置里的 logs 文件夹（用 id 定位），不碰其它文件夹

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CONFIG="$ROOT/config.json"
TARGET="2026-10-03"

while [ $# -gt 0 ]; do
  case "$1" in
    --title) TARGET="${2:-}"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [ ! -f "$CONFIG" ]; then
  echo "❌ 缺少 config.json，请先运行：bash deploy/init.sh" >&2
  exit 1
fi

FOLDER_ID=$(/usr/bin/python3 -c "
import json
print(json.load(open('$CONFIG'))['folder_id'])
")

run() { osascript -e "$1" 2>&1; }

echo "删除当天页：「$TARGET」"
echo "════════════════════════════════════════"

# 先确认文件夹有效
CHECK=$(run "tell application \"Notes\"
  set hits to (every folder whose id \"$FOLDER_ID\")
  if (count of hits) is 0 then return \"MISSING\"
  set f to item 1 of hits
  return (name of f) & \"|\" & (count of notes of f)
end tell")
if [ "$CHECK" = "MISSING" ]; then
  echo "❌ 配置里的日志文件夹不存在了，请重新运行 init.sh" >&2
  exit 1
fi
echo "日志文件夹：$CHECK"

BEFORE=$(echo "$CHECK" | cut -d'|' -f2)

echo
echo "── 匹配情况（精确匹配标题）──"
FOUND=$(run "tell application \"Notes\"
  set out to \"\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    if (name of n) is \"$TARGET\" then set out to out & \"  找到：\" & (name of n) & linefeed
  end repeat
  if out is \"\" then return \"  （没有标题正好是「$TARGET」的条目）\"
  return out
end tell")
echo "$FOUND"

if echo "$FOUND" | grep -q "没有标题正好是"; then
  echo
  echo "✅ 无需删除。"
  exit 0
fi

echo
echo "── 执行删除 ──"
RESULT=$(run "tell application \"Notes\"
  set removed to 0
  repeat with i from (count of notes of folder id \"$FOLDER_ID\") to 1 by -1
    try
      if (name of note i of folder id \"$FOLDER_ID\") is \"$TARGET\" then
        delete note i of folder id \"$FOLDER_ID\"
        set removed to removed + 1
      end if
    end try
  end repeat
  return \"已删除 \" & removed & \" 条\"
end tell")
echo "  $RESULT"

AFTER=$(run "tell application \"Notes\"
  return count of notes of folder id \"$FOLDER_ID\"
end tell" | tr -d ' \n')

echo
echo "── 结果 ──"
echo "  条数：$BEFORE → $AFTER"

echo
echo "── 复核：还有没有标题正好是「$TARGET」的 ──"
run "tell application \"Notes\"
  set out to \"\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    if (name of n) is \"$TARGET\" then set out to out & \"  ⚠️ 仍存在：\" & (name of n) & linefeed
  end repeat
  if out is \"\" then return \"  ✅ 已无匹配\"
  return out
end tell"

echo
echo "剩下的条目："
run "tell application \"Notes\"
  set out to \"\"
  repeat with n in notes of folder id \"$FOLDER_ID\"
    set out to out & \"  · \" & (name of n) & linefeed
  end repeat
  return out
end tell"

echo
echo "（注意：备忘录的删除是软删除，会在「最近删除」里保留 30 天，属正常）"
