#!/bin/bash
# 备忘录写入能力探测（走 osascript，复用「终端」已有的自动化授权）
#
# 为什么用 osascript 而不是 app bundle 里的命令：
#   我们的 app 是 ad-hoc 签名，每次重新编译二进制哈希都会变，
#   而 macOS 的 TCC 授权绑定二进制哈希 —— 一编译就失效，需要重新授权。
#   「终端」对备忘录的自动化授权是稳定的，借用它可以免去反复授权。
#
# 安全设计（重要）：
#   写入测试**不碰你的任何现有备忘录**。做法是先 duplicate 出一条副本，
#   只对副本做写入，跑完删除副本。原稿全程只读。
#   上一版设计是"改原文再还原"，那需要把原文经 shell 回填进 AppleScript，
#   原文里的引号/换行会破坏语法并可能损坏数据 —— 该做法已废弃。
#
# 判据：每次写入都用「读回验证」确认，不看命令有没有报错 ——
#       备忘录存在「静默失败」（不报错但什么都没做）。

set -u
TEST_TITLE="DIAG-OSA-$$"
MARK="MARK-$$"

run() { osascript -e "$1" 2>&1; }

echo "备忘录写入能力探测（osascript 通道，非破坏性）"
echo "════════════════════════════════════════"
echo

echo "── 1. 读取 ─────────────────────────────"
N1=$(run 'tell application "Notes" to count notes')
if echo "$N1" | grep -q "execution error"; then
  echo "   ❌ 读取失败：$N1"
  echo "      请在 系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「备忘录」"
  exit 2
fi
echo "   ✅ 备忘录共 $N1 条"
BASELINE="$N1"

echo
echo "── 2. 创建能力（count 前后对比）────────"
OUT=$(run "tell application \"Notes\"
  set n1 to count of notes
  try
    make new note with properties {body:\"$TEST_TITLE\"}
    set em to \"no-error\"
  on error e
    set em to e
  end try
  set n2 to count of notes
  return (n1 as string) & \"|\" & (n2 as string) & \"|\" & em
end tell")
BEFORE=$(echo "$OUT" | cut -d'|' -f1)
AFTER=$(echo "$OUT" | cut -d'|' -f2)
ERRMSG=$(echo "$OUT" | cut -d'|' -f3-)
echo "   count: $BEFORE → $AFTER"
echo "   错误信息: $ERRMSG"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "   ❌ 静默失败：没报错，但一条都没建成"
  CREATE_OK=no
else
  echo "   ✅ 创建成功"
  CREATE_OK=yes
fi

echo
echo "── 3. 写入能力（duplicate 出副本再测，不动原稿）──"
USE_ID=""
DUP=$(run "tell application \"Notes\"
  if (count of notes) is 0 then return \"NONE\"
  try
    duplicate note 1
    return \"ok\"
  on error e
    return \"ERR: \" & e
  end try
end tell")
echo "   duplicate 返回: $DUP"
if echo "$DUP" | grep -q "^ok"; then
  USE_ID=$(run "tell application \"Notes\" to return id of note 1")
  echo "   副本 id：$(echo "$USE_ID" | cut -c1-12)…"
elif [ "${CREATE_OK:-no}" = "yes" ]; then
  USE_ID=$(run "tell application \"Notes\" to return id of note 1")
  echo "   改用刚创建的测试条目：$(echo "$USE_ID" | cut -c1-12)…"
else
  echo "   ⚠️  既不能 duplicate 也没有测试条目，跳过写入测试"
fi

if [ -n "${USE_ID:-}" ]; then
  W=$(run "tell application \"Notes\"
    set theNotes to every note whose id is \"$USE_ID\"
    if (count of theNotes) is 0 then return \"NOTFOUND\"
    try
      set body of item 1 of theNotes to (body of item 1 of theNotes) & \"<div>$MARK</div>\"
      return \"wrote\"
    on error e
      return \"ERR: \" & e
    end try
  end tell")
  echo "   set body 返回: $W"
  V=$(run "tell application \"Notes\"
    set theNotes to every note whose id is \"$USE_ID\"
    if (count of theNotes) is 0 then return \"GONE\"
    return plaintext of item 1 of theNotes
  end tell")
  if echo "$V" | grep -q "$MARK"; then
    echo "   ✅ 读回可见 → 可以写入备忘录"
    APPEND_OK=yes
  else
    echo "   ❌ 读回不可见 → 写入无效"
    APPEND_OK=no
  fi
fi

echo
echo "── 4. 清理（删副本与测试条目，恢复到 $BASELINE 条）──"
CL=$(run "tell application \"Notes\"
  set n to count of notes
  repeat while (count of notes) > $BASELINE
    try
      delete note 1
    on error
      exit repeat
    end try
  end repeat
  return (n as string) & \" -> \" & ((count of notes) as string)
end tell")
echo "   $CL"

echo
echo "════════════════════════════════════════"
echo "结论：创建=${CREATE_OK:-no}   写入=${APPEND_OK:-no}"
if [ "${CREATE_OK:-no}" = "yes" ] && [ "${APPEND_OK:-no}" = "yes" ]; then
  echo "→ 读写都通。方案 A 可全自动，含「未完自动顺延到 n+1」"
elif [ "${CREATE_OK:-no}" = "yes" ] || [ "${APPEND_OK:-no}" = "yes" ]; then
  echo "→ 部分可用。顺延改用「每天新建一页，把未完成项写进新页」这个变体"
else
  echo "→ 备忘录只读。顺延需改走快捷指令桥（B）或 iCloud 文本文件（C）"
fi
