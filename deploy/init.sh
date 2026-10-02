#!/bin/bash
# pdca 初始化：定位日志文件夹并记下它的 id
#
# 为什么不用文件夹名字定位：
#   用户新建的日志文件夹曾叫 [Note]，而系统原本就有 [Notes]（21 条真实笔记）。
#   两个名字只差一个字母，靠名字定位有三个隐患：
#     · 改名即失效
#     · 出现第二个同名文件夹时选到哪个不确定
#     · 人工核对日志时容易看错
#   所以这里把**文件夹 id** 记进 config.json，之后一律用 id 定位。
#
# 定位优先级：
#   1. config.json 里已有的 folder_id —— 验证它还在，在就直接用（改名不受影响）
#   2. 按 --name 指定的名字查找 —— 要求**唯一匹配**，撞车就报错让人裁决
#   3. 都没有 → 列出所有文件夹让用户指定
#
# 用法：
#   bash deploy/init.sh              # 用默认名字 logs 查找
#   bash deploy/init.sh --name 某个名字
#   bash deploy/init.sh --list       # 只列出账户与文件夹，不写配置

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CONFIG="$ROOT/config.json"
TARGET_NAME="logs"
LIST_ONLY=no

while [ $# -gt 0 ]; do
  case "$1" in
    --name) TARGET_NAME="${2:-}"; shift 2 ;;
    --list) LIST_ONLY=yes; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

run() { osascript -e "$1" 2>&1; }

echo "pdca 初始化"
echo "════════════════════════════════════════"

# ── 先确认备忘录可访问（借用终端的自动化授权）
PROBE=$(run 'tell application "Notes" to count notes')
if echo "$PROBE" | grep -q "execution error"; then
  echo "❌ 无法访问备忘录：$PROBE"
  echo "   请到 系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「备忘录」"
  exit 2
fi
echo "✅ 备忘录可访问"

# ── 列出当前结构
echo
echo "── 当前账户与文件夹 ────────────────────"
run 'tell application "Notes"
  set out to ""
  repeat with a in accounts
    set out to out & "账户 [" & (name of a) & "]" & linefeed
    repeat with f in folders of a
      set out to out & "   [" & (name of f) & "]  " & (count of notes of f) & " 条   id=" & (id of f) & linefeed
    end repeat
  end repeat
  return out
end tell'

if [ "$LIST_ONLY" = "yes" ]; then
  echo
  echo "（--list 模式，不写配置）"
  exit 0
fi

# ── 1. 先看已有配置是否仍然有效
echo
echo "── 解析目标文件夹 ──────────────────────"
RESOLVED_ID=""
RESOLVED_NAME=""
RESOLVED_COUNT=""

if [ -f "$CONFIG" ]; then
  OLD_ID=$(python3 -c "
import json,sys
try:
    print(json.load(open('$CONFIG')).get('folder_id',''))
except Exception:
    print('')
" 2>/dev/null)
  if [ -n "${OLD_ID:-}" ]; then
    CHECK=$(run "tell application \"Notes\"
      set hits to (every folder whose id is \"$OLD_ID\")
      if (count of hits) is 0 then return \"MISSING\"
      set f to item 1 of hits
      return (name of f) & \"|\" & (count of notes of f)
    end tell")
    if [ "$CHECK" != "MISSING" ] && ! echo "$CHECK" | grep -q "execution error"; then
      RESOLVED_ID="$OLD_ID"
      RESOLVED_NAME=$(echo "$CHECK" | cut -d'|' -f1)
      RESOLVED_COUNT=$(echo "$CHECK" | cut -d'|' -f2)
      echo "   ✅ 已有配置的 folder_id 仍然有效（当前名称「${RESOLVED_NAME}」，${RESOLVED_COUNT} 条）"
      echo "      → 说明即使你改过名，定位也没断（这正是用 id 的意义）"
    else
      echo "   ⚠️  配置里的 folder_id 已失效（文件夹可能被删或重建）"
      echo "      改为按名字查找……"
    fi
  fi
fi

# ── 2. 按名字查找（要求唯一）
if [ -z "$RESOLVED_ID" ]; then
  LOOKUP=$(run "tell application \"Notes\"
    set hits to {}
    repeat with a in accounts
      repeat with f in folders of a
        if (name of f) is \"$TARGET_NAME\" then set end of hits to f
      end repeat
    end repeat
    if (count of hits) is 0 then return \"NONE\"
    if (count of hits) > 1 then return \"AMBIGUOUS:\" & (count of hits)
    set f to item 1 of hits
    return (id of f) & \"|\" & (name of f) & \"|\" & (count of notes of f)
  end tell")

  case "$LOOKUP" in
    NONE)
      echo "   ❌ 没找到名为「${TARGET_NAME}」的文件夹。"
      echo "      请先在备忘录里建好它，或用 --name 指定其它名字。"
      echo "      （当前可用文件夹见上面的列表）"
      exit 1
      ;;
    AMBIGUOUS:*)
      N=$(echo "$LOOKUP" | cut -d: -f2)
      echo "   ❌ 有 ${N} 个文件夹都叫「${TARGET_NAME}」，无法确定用哪个。"
      echo "      请给其中之一改名以消除歧义 —— 这正是不能用名字定位的原因。"
      exit 1
      ;;
    *)
      RESOLVED_ID=$(echo "$LOOKUP" | cut -d'|' -f1)
      RESOLVED_NAME=$(echo "$LOOKUP" | cut -d'|' -f2)
      RESOLVED_COUNT=$(echo "$LOOKUP" | cut -d'|' -f3)
      echo "   ✅ 按名字「${TARGET_NAME}」找到唯一匹配（${RESOLVED_COUNT} 条）"
      ;;
  esac
fi

# ── 3. 写配置
python3 - "$CONFIG" "$RESOLVED_ID" "$RESOLVED_NAME" <<'PY'
import json, sys, datetime
path, fid, fname = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = {}
try:
    cfg = json.load(open(path))
except Exception:
    pass
cfg.update({
    "folder_id": fid,
    "folder_name": fname,
    "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
})
# 这些是给实现用的默认值，先写进去，之后可改
cfg.setdefault("folder_name_hint", fname)
json.dump(cfg, open(path, "w"), ensure_ascii=False, indent=2)
print("   已写入配置：" + path)
PY

echo
echo "── 配置内容 ────────────────────────────"
cat "$CONFIG"
echo
echo "✅ 初始化完成。"
echo "   之后无论你把「${RESOLVED_NAME}」改成什么名字，定位都不会断。"
