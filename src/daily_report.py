#!/usr/bin/env python3
"""
21:30 日报：汇总当天进度，推送提醒你补打钩。

为什么这条推送必须存在：
  顺延依赖「完成状态」准确 —— 只有你打了钩，程序才知道哪些没做完。
  如果没人提醒，你很可能会忘；忘了打钩，做完的事就会被顺延到明天。
  所以这条推送不是"催办"，它是**整条链路的数据质量保障**。

措辞原则：不催、不评判。你打钩的动力来自"看到自己完成了什么"，
不是来自"被机器指责还剩几件"。所以完成项放在前面。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError  # noqa: E402
import notify  # noqa: E402
import parse as parser  # noqa: E402
import sync  # noqa: E402


def build_report(date_str: str, result: parser.ParseResult) -> tuple[str, str]:
    """生成 (标题, 正文)。"""
    todos = result.todos
    done = [e for e in todos if e.completed is True]
    open_items = [e for e in todos if e.completed is not True]
    notes_count = len(result.notes)

    total = len(todos)
    title = f"📋 {date_str} 今日复盘"

    if total == 0:
        body = "今天没有登记待办。\n\n如果只想记点东西，用 * 开头写备忘。"
        return title, body

    lines: list[str] = []

    # 完成项放前面：让人先看到成果
    if done:
        lines.append(f"✅ 已完成 {len(done)}/{total}")
        for e in done:
            label = f"　{e.slot} " if e.slot else "　"
            lines.append(f"{label}{e.text}")
        lines.append("")

    if open_items:
        lines.append(f"⏳ 未完成 {len(open_items)} 件")
        for e in open_items:
            label = f"　{e.slot} " if e.slot else "　"
            lines.append(f"{label}{e.text}")
        lines.append("")
        lines.append("做完的请在备忘录里打上勾 ✓")
        lines.append("没打钩的明早会顺延到次日页")
    else:
        lines.append("🎉 今天的待办全部完成")

    if notes_count:
        lines.append("")
        lines.append(f"（另有 {notes_count} 条备忘）")

    return title, "\n".join(lines)


def note_url(note_id: str) -> str | None:
    """
    生成点击直达备忘录的链接。

    格式参考 x-coredata id：applenotes://showNote?identifier=<id>
    这是**未经验证**的猜测 —— 所以做成"可选增强"：如果点开无效，
    不影响日报本身（正文里已有全部信息）。
    """
    if not note_id or "x-coredata://" not in note_id:
        return None
    return f"applenotes://showNote?identifier={note_id}"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成并推送当天日报")
    ap.add_argument("date", nargs="?", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--no-push", action="store_true", help="只打印，不推送")
    ap.add_argument("--refresh", action="store_true",
                    help="推送前先从备忘录重新读取（默认读留档快照）")
    args = ap.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{date_str}", file=sys.stderr)
        return 1

    # 取数据：默认读留档；--refresh 时重新同步
    result: parser.ParseResult | None = None
    meta: dict = {}
    if args.refresh:
        try:
            s = sync.sync_day(date_str)
        except NotesError as e:
            print(f"❌ 读取备忘录失败：{e}", file=sys.stderr)
            return 2
        if s is None:
            print(f"⚠️  {date_str} 没有当天页")
            return 1
        result, meta = s.result, {"note_id": s.note_id}
    else:
        result = sync.load_archive(date_str)
        meta = sync.archive_meta(date_str)
        if result is None:
            print(f"没有 {date_str} 的留档。加 --refresh 从备忘录读取，"
                  f"或先跑 src/read_day.py {date_str}", file=sys.stderr)
            return 1

    title, body = build_report(date_str, result)

    print(f"标题：{title}")
    print("─" * 46)
    print(body)
    print("─" * 46)

    if args.no_push:
        return 0

    url = note_url(meta.get("note_id", ""))
    ok, msg = notify.send_bark(title, body, url=url)
    print()
    print(("✅ " if ok else "❌ ") + msg)
    if not ok:
        # 推送失败不改变退出码语义之外的行为：日报内容已经打出来了，
        # 但明确告知失败，避免"以为推了其实没推"。
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
