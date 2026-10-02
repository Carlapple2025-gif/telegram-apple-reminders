#!/bin/bash
# 探测文件夹 id（决定「怎么稳定地定位日志文件夹」，避免靠名字）
#
# 背景：用户新建了 [Note] 存放日志，而系统原本就有 [Notes]（21 条）。
# 两个名字只差一个字母，靠名字定位很脆：
#   · 改名就失效
#   · 出现同名文件夹时选到哪个不确定
# 正确做法是用文件夹的唯一 id 定位。
#
# 本脚本只读。

set -u
run() { osascript -e "$1" 2>&1; }

echo "文件夹身份探测（只读）"
echo "════════════════════════════════════════"
echo

echo "── 账户与文件夹的 id / 名称 / 条数 ────"
run 'tell application "Notes"
  set out to ""
  repeat with a in accounts
    set out to out & "账户 [" & (name of a) & "]" & linefeed
    set out to out & "   id: " & (id of a) & linefeed
    repeat with f in folders of a
      set out to out & "   文件夹 [" & (name of f) & "]" & linefeed
      set out to out & "      id:     " & (id of f) & linefeed
      set out to out & "      条数:   " & (count of notes of f) & linefeed
      try
        set out to out & "      容器:   " & (name of container of f) & linefeed
      on error
        set out to out & "      容器:   <读不到>" & linefeed
      end try
    end repeat
  end repeat
  return out
end tell'

echo
echo "── 用 id 反查文件夹是否可靠 ────────────"
run 'tell application "Notes"
  set out to ""
  repeat with a in accounts
    repeat with f in folders of a
      if (name of f) is "Note" then
        set fid to id of f
        set out to out & "  目标文件夹 id: " & fid & linefeed
        -- 用 id 反查
        set hits to (every folder whose id is fid)
        set out to out & "  按 id 反查得到: " & (count of hits) & " 个" & linefeed
        -- 用名字反查（对比：看会不会撞车）
        set nameHits to (every folder whose name is "Note")
        set out to out & "  按 name 反查得到: " & (count of nameHits) & " 个" & linefeed
        -- 该文件夹里的条目
        set out to out & "  其中条目: " & (count of notes of f) & " 条" & linefeed
        repeat with nn in notes of f
          set out to out & "     · " & (name of nn) & "   id=" & (id of nn) & linefeed
        end repeat
      end if
    end repeat
  end repeat
  if out is "" then return "  ⚠️ 没找到名为 Note 的文件夹"
  return out
end tell'
