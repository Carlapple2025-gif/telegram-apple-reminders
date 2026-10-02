#!/usr/bin/env python3
"""
探测：哪种 HTML 结构能在备忘录里渲染成「可点的勾选框」。

背景：实测发现用户手动转出来的清单项在 HTML 里是普通 `<ul><li>`，
纯文本 `[ ]` 仍保留，`<li>` 上没有任何状态标记。所以要么它本来就是普通列表，
要么 `body` 属性返回的是简化版 HTML。备忘录的清单结构是私有实现，
没有公开文档，只能实测。

为什么用 Python 而不是 bash 写这个脚本：
    上一版用 bash 拼 AppleScript 字符串，HTML 里的双引号（`class="..."`）
    没转义，直接导致 AppleScript 语法错（-2741）。`src/notes.py` 里已经有
    正确的转义实现，复用它就不会再犯 —— 这也说明"同一件事有两份实现"
    本身就是 bug 来源。

安全：只新建一条标题含 FMT-TEST 的测试笔记，不碰其它内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError, run_applescript, _as_literal  # noqa: E402


def build_candidates(title: str) -> list[tuple[str, str]]:
    """返回 [(说明, HTML 片段)]。每行一个候选，方便肉眼逐行对照。"""
    return [
        ("表头", f"<div>{title}　请对照下面各行，告诉我哪几行是可点的勾选框</div>"),
        ("A 普通列表", '<ul><li>A：普通 ul/li（你手动转出来的那种）</li></ul>'),
        ("A2 普通列表+纯文本标记",
         '<ul><li>A2：普通 li，文字里带 - [ ] 标记</li></ul>'),
        ("B Apple-checklist 类",
         '<ul class="Apple-checklist"><li>B：ul class=Apple-checklist</li></ul>'),
        ("B2 Apple-checklist + done",
         '<ul class="Apple-checklist"><li class="done">B2：li class=done（应为已勾选）</li></ul>'),
        ("C li class=checklist",
         '<ul><li class="checklist">C：li class=checklist</li></ul>'),
        ("D data-checked 属性",
         '<ul><li data-checked="false">D：data-checked=false</li>'
         '<li data-checked="true">D2：data-checked=true</li></ul>'),
        ("E 备忘录新版的 checklist 类名尝试",
         '<ul class="checklist"><li>E：ul class=checklist</li></ul>'),
    ]


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="探测可渲染成勾选框的 HTML 结构")
    ap.add_argument("--title", help="测试笔记标题（默认 FMT-TEST-<pid>）")
    args = ap.parse_args()

    title = args.title or f"FMT-TEST-{__import__('os').getpid()}"

    try:
        notes = Notes()
        folder_name, _ = notes.verify_folder()
    except NotesError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    print(f"日志文件夹：「{folder_name}」")
    print(f"创建测试笔记：「{title}」")
    print("═" * 56)

    body = "".join(html for _, html in build_candidates(title))

    # 用 notes.create 的核心逻辑，但正文需要自定义 HTML（不是逐行 <div> 包裹），
    # 所以直接调底层，并复用 _as_literal 做转义 —— 这正是上一版 bash 脚本漏掉的。
    try:
        run_applescript(
            'tell application "Notes"\n'
            f'  make new note at folder id {_as_literal(notes.folder_id)} '
            f'with properties {{body:{_as_literal(body)}}}\n'
            '  return "made"\n'
            'end tell'
        )
    except NotesError as e:
        print(f"❌ 创建失败：{e}", file=sys.stderr)
        return 3

    # 读回验证
    note = notes.get(title)
    if note is None:
        print("❌ 创建命令没报错，但读回找不到 —— 写入未生效", file=sys.stderr)
        return 3

    print("✅ 已创建")
    print()
    print("── plaintext（读回的样子）──")
    print(note.plaintext)
    print()
    print("── body（备忘录如何改写我们的 HTML）──")

    body_back = run_applescript(
        'tell application "Notes"\n'
        f'  set hits to (every note of folder id {_as_literal(notes.folder_id)} '
        f'whose id is {_as_literal(note.id)})\n'
        '  if (count of hits) is 0 then return "NOTFOUND"\n'
        '  return body of item 1 of hits\n'
        'end tell'
    )
    print(body_back)

    print()
    print("═" * 56)
    print(f"请在备忘录里打开「{title}」，回答：")
    print("  A / A2 / B / B2 / C / D / D2 / E 各行里，")
    print("  哪些显示成**可点的勾选框**？哪些只是普通圆点列表？")
    print()
    print(f"用完删除：bash deploy/delete-day.sh --title {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
