#!/usr/bin/env python3
# ════════════════════════════════════════════════════════════════════════
# ⚠️ v1 遗留代码 —— **v4 不调用本文件**
#
# v4 的唯一输入入口是 Telegram（daemon.py → intake.py → reminders/applecal/memo），
# 已没有"同步""顺延""三处状态合并"这些概念。本文件属于被推翻的 v1 方案，
# 保留原因：v1 是唯一能读写备忘录正文的代码，万一要复用不必翻 git 历史。
#
# 重新启用前请先读 docs/ARCHITECTURE.md 的「为什么推翻 v1」——
# 这批代码的共同根因是**一条状态存在三处**，于是不得不"猜身份"
# （行号 / 原样文字 / ⟳ 标记），并因此产生四次同源 bug。
# ════════════════════════════════════════════════════════════════════════
"""
读取当天页 → 解析 → 落成留档文件。

这是**只读**操作：只读备忘录、只写本仓库的 data/ 目录，不修改你的备忘录。

留档的意义：备忘录只是书写界面，不是数据库。它可能因为授权失效、iCloud
不同步、误删而读不到。每天一份独立副本，出问题时你能看见丢了什么，
而不是数据悄无声息地没了。

用法：
    python3 src/read_day.py                 # 读今天
    python3 src/read_day.py 2026-10-02      # 读指定日期
    python3 src/read_day.py --print         # 只打印，不写留档
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError  # noqa: E402
import parse as parser  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAYS_DIR = ROOT / "data" / "days"


def main() -> int:
    ap = argparse.ArgumentParser(description="读取当天页并留档")
    ap.add_argument("date", nargs="?", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--print", dest="only_print", action="store_true",
                    help="只打印解析结果，不写留档文件")
    args = ap.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{date_str}（应为 YYYY-MM-DD）", file=sys.stderr)
        return 1

    try:
        notes = Notes()
        folder_name, total = notes.verify_folder()
    except NotesError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    print(f"日志文件夹：「{folder_name}」（{total} 条）")

    try:
        note = notes.find_daily_page(date_str)
    except NotesError as e:
        print(f"❌ 读取失败：{e}", file=sys.stderr)
        return 2

    if note is None:
        print(f"⚠️  该文件夹内没有以「{date_str}」开头的条目。")
        print(f"    请先在备忘录的「{folder_name}」文件夹里建一条，标题为 {date_str}")
        return 1

    print(f"找到当天页：{note.name}")
    print(f"  条目 id: {note.id}")

    result = parser.parse(note.plaintext)
    if result.date is None:
        result.date = date_str

    # 打印给人看的结果
    print()
    print("── 解析结果 " + "─" * 46)
    labels = {"todo": "待办", "note": "备忘", "meta": "元信息", "unknown": "待定"}
    for e in result.entries:
        kind = labels.get(e.kind, e.kind)
        state = ""
        if e.kind == "todo":
            state = " ✓已完成" if e.completed else " ☐未完成"
        warn = ("  ⚠️ " + "；".join(e.issues)) if e.issues else ""
        print(f"  [{kind}]{state}  {e.text}{warn}")

    print()
    print(f"  待办 {len(result.todos)} 条（未完成 {len(result.open_todos)} 条）"
          f" · 备忘 {len(result.notes)} 条")

    dups = parser.find_duplicates(result.entries)
    if dups:
        print()
        print("  ⚠️ 疑似重复（需你确认，程序不会自动删）：")
        for a, b, r in dups:
            print(f"     第 {a} 行 与 第 {b} 行，相似度 {r}")

    if args.only_print:
        return 0

    # 写留档
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAYS_DIR / f"{date_str}.md"
    meta = {
        "date": date_str,
        "source": f"备忘录 [{folder_name}] / {note.name}",
        "note_id": note.id,
        "read_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    out_path.write_text(parser.render_archive(result, meta), encoding="utf-8")

    # 同时写一份机器可读的 JSON，供后续日报/顺延使用
    json_path = DAYS_DIR / f"{date_str}.json"
    payload = result.to_dict()
    payload["_meta"] = meta
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print(f"✅ 留档已写入：")
    print(f"   {out_path.relative_to(ROOT)}")
    print(f"   {json_path.relative_to(ROOT)}")
    print()
    print("请打开上面那个 .md 文件核对两件事：")
    print("  1. 「原文」段落与你在备忘录里写的是否逐字一致")
    print("  2. 「解析结果」表格有没有把你的行归错类")
    return 0


if __name__ == "__main__":
    sys.exit(main())
