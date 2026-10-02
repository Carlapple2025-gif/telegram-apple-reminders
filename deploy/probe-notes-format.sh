#!/bin/bash
# 备忘录格式与清理（第三轮）
#
# 背景：第二轮结构探测发现两个问题
#   1. 脚本因 `set -u` + 命令替换内的变量赋值而提前退出，**清理没跑成** ——
#      可能残留一条标题为 STRUCT-PROBE-<pid> 的测试备忘录。本脚本先补这个清理。
#   2. 写进去的多行内容被**折叠成一行**（plaintext 读回是
#      "标题 - [ ] 待办甲 - [x] 待办乙 * 备忘丙 @中午 时段丁"）。
#      这关系到当天页的可读性，也关系到解析器能不能按行切分 —— 本脚本把
#      两种分行的写法各试一次，看哪种能真正产生换行。
#
# 安全：只操作自己新建的测试条目，跑完删除并核对条数回到基线。

set -u
TEST_TITLE="FMT-PROBE-$$"
run() { osascript -e "$1" 2>&1; }

echo "备忘录格式测试与清理"
echo "════════════════════════════════════════"
echo

echo "── 0. 先清理上一轮可能残留的测试条目 ──"
run 'tell application "Notes"
  set out to ""
  set n to count of notes
  set removed to 0
  repeat with i from n to 1 by -1
    try
      set nm to name of note i
      if nm starts with "STRUCT-PROBE-" or nm starts with "DIAG-OSA-" or nm starts with "FMT-PROBE-" then
        delete note i
        set removed to removed + 1
      end if
    end try
  end repeat
  return "清理了 " & removed & " 条，现有 " & (count of notes) & " 条"
end tell'

BASELINE=$(run 'tell application "Notes" to count notes' | tr -d ' \n')
echo "   基线：$BASELINE 条"

echo
echo "── 1. 分行测试 A：用 <div> 包每一行 ────"
ID_A=$(run "tell application \"Notes\"
  make new note with properties {body:\"$TEST_TITLE-A<div>- [ ] 甲</div><div>- [x] 乙</div><div>* 丙</div>\"}
  return id of note 1
end tell")
echo "   读回 plaintext："
run "tell application \"Notes\"
  set t to every note whose id is \"$ID_A\"
  if (count of t) is 0 then return \"<<NOTFOUND>>\"
  return plaintext of item 1 of t
end tell" | sed 's/^/      | /'

echo
echo "── 2. 分行测试 B：用 <br> 分隔 ─────────"
ID_B=$(run "tell application \"Notes\"
  make new note with properties {body:\"$TEST_TITLE-B<br>- [ ] 甲<br>- [x] 乙<br>* 丙\"}
  return id of note 1
end tell")
echo "   读回 plaintext："
run "tell application \"Notes\"
  set t to every note whose id is \"$ID_B\"
  if (count of t) is 0 then return \"<<NOTFOUND>>\"
  return plaintext of item 1 of t
end tell" | sed 's/^/      | /'

echo
echo "── 3. 追加测试：往已有条目追加一行（顺延的核心动作）──"
MARK="APPEND-$$"
run "tell application \"Notes\"
  set t to every note whose id is \"$ID_A\"
  if (count of t) is 0 then return \"GONE\"
  set body of item 1 of t to (body of item 1 of t) & \"<div>$MARK</div>\"
  return \"appended\"
end tell"
echo "   读回验证是否可见："
run "tell application \"Notes\"
  set t to every note whose id is \"$ID_A\"
  if (count of t) is 0 then return \"<<NOTFOUND>>\"
  set pt to plaintext of item 1 of t
  if pt contains \"$MARK\" then
    return \"✅ 追加可见\" & linefeed & pt
  else
    return \"❌ 追加不可见\" & linefeed & pt
  end if
end tell" | sed 's/^/      | /'

echo
echo "── 4. 清理两个测试条目 ─────────────────"
run 'tell application "Notes"
  set out to ""
  set removed to 0
  repeat with i from (count of notes) to 1 by -1
    try
      set nm to name of note i
      if nm starts with "FMT-PROBE-" then
        delete note i
        set removed to removed + 1
      end if
    end try
  end repeat
  return "删除 " & removed & " 条，剩余 " & (count of notes) & " 条"
end tell'
echo "   （基线是 $BASELINE 条）"
