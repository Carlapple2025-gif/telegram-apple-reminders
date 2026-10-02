#!/bin/bash
# 备忘录结构探测：为「n+1 顺延」的写入实现摸清容器与定位方式。
#
# 需要确认四件事（前两个探测都没解决）：
#   1. 备忘录到底在哪个容器 —— 第一次探测显示「every folder 返回空」，
#      但明明有 22 条，说明它挂在 account 下面而不是顶层。
#   2. id 的真实格式 —— 上一轮打印出 "x-coredata:/…"，需要看全文才能确认
#      `every note whose id is "..."` 这种定位是否可靠。
#   3. 新建的备忘录默认落在哪里（顺延写入的目标位置）。
#   4. 写入 `- [ ]` 标记后，读回来是纯文本还是被转成 HTML 清单 ——
#      这决定解析器按文本解析还是按 HTML 解析。
#
# 本脚本只读，不修改任何内容（唯一的写入是对一条**新建的测试条目**，
# 跑完删除）。

set -u
TEST_TITLE="STRUCT-PROBE-$$"
MARK="STRUCT-MARK-$$"
run() { osascript -e "$1" 2>&1; }

echo "备忘录结构探测（只读为主）"
echo "════════════════════════════════════════"
echo

echo "── 1. 账户与文件夹层级 ─────────────────"
run "tell application \"Notes\"
  set out to \"\"
  set out to out & \"账户数: \" & (count of accounts) & linefeed
  repeat with a in accounts
    set out to out & \"账户[\" & (name of a) & \"] 文件夹数: \" & (count of folders of a) & linefeed
    repeat with f in folders of a
      set out to out & \"   - \" & (name of f) & \"  (notes: \" & (count of notes of f) & \")\" & linefeed
    end repeat
    set out to out & \"   直属 notes: \" & (count of notes of a) & linefeed
  end repeat
  return out
end tell" | sed 's/^/   /'

BASELINE=$(run 'tell application "Notes" to count notes' | tr -d ' ')
echo
echo "── 2. 备忘录 id 与容器的真实形态 ───────"
run "tell application \"Notes\"
  set out to \"\"
  repeat with i from 1 to 2
    set n to note i
    set out to out & \"--- note \" & i & linefeed
    set out to out & \"  id: \" & (id of n) & linefeed
    set out to out & \"  name: \" & (name of n) & linefeed
    set out to out & \"  container: \" & (name of container of n) & linefeed
    set out to out & \"  modified: \" & (modification date of n as string) & linefeed
  end repeat
  return out
end tell" | sed 's/^/   /'

echo
echo "── 3. 标记往返：写 - [ ] 进去，读回看是什么 ──"
NEWID=$(run "tell application \"Notes\"
  set n1 to count of notes
  make new note with properties {body:\"$TEST_TITLE
- [ ] 待办甲
- [x] 待办乙
* 备忘丙
@中午 时段丁\"}
  return id of note 1
end tell")
echo "   新建条目 id: $(echo "$NEWID" | cut -c1-40)…"
echo
echo "   读回 plaintext："
run "tell application \"Notes\"
  set theNotes to every note whose id is \"$NEWID\"
  if (count of theNotes) is 0 then return \"<<NOTFOUND>>\"
  return plaintext of item 1 of theNotes
end tell" | sed 's/^/      | /'
echo
echo "   读回 body（原始 HTML，截断）："
run "tell application \"Notes\"
  set theNotes to every note whose id is \"$NEWID\"
  if (count of theNotes) is 0 then return \"<<NOTFOUND>>\"
  set b to body of item 1 of theNotes
  if (length of b) > 600 then set b to (text 1 thru 600 of b) & \"...[截断]\"
  return b
end tell" | sed 's/^/      | /'

echo
echo "── 4. 定位可靠性：用 id / name / first note ──"
echo -n "   按 id 定位条数: "
run "tell application \"Notes\" to return count of (every note whose id is \"$NEWID\")"
echo -n "   按 name 定位条数: "
run "tell application \"Notes\" to return count of (every note whose name is \"$TEST_TITLE\")"

echo
echo "── 5. 清理 ─────────────────────────────"
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
echo "   ${CL}（基线应为 ${BASELINE}）"
