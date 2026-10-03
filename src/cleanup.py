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
清理日志文件夹里的测试残留与提前生成的页面。

为什么用 Python 重写（原先是个 bash 脚本）：
    bash 版本需要把「匹配条件」拼进 AppleScript 表达式，反复因为引号转义
    出错（-2741 语法错）。而且那种拼法把「哪些该删」的逻辑藏在一层字符串
    拼接里，出错时看不出真相。
    现在改成：**在 Python 里筛选，AppleScript 只接受一个具体 id**。
    转义交给已验证的 _as_literal，判定逻辑留在 Python 里、可读可测。

安全原则：
   · 默认**只列出不删除**，加 --apply 才真删
   · 只删符合"明确测试前缀"或"日期晚于今天"的条目
   · 逐条打印将要删除的标题，让你核对
   · 删完复查
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError  # noqa: E402

# 已知的测试前缀 —— 覆盖这个项目历次探测留下的所有可能残留
TEST_PREFIXES = (
    "FMT-TEST-",
    "STRUCT-PROBE-",
    "DIAG-",
    "NOTES-PROBE-",
    "# PDCA probe",
)

DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def classify(name: str, today: dt.date) -> str | None:
    """返回删除理由；None 表示保留。"""
    for pfx in TEST_PREFIXES:
        if name.startswith(pfx):
            return f"测试残留（前缀 {pfx}）"

    if DATE_ONLY.match(name):
        try:
            d = dt.date.fromisoformat(name)
        except ValueError:
            return None
        if d > today:
            return f"提前生成的页面（{name} 晚于今天 {today.isoformat()}）"
        # 今天或更早的当天页一律保留
        return None

    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="清理测试残留与提前生成的页面")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列出）")
    ap.add_argument("--today", help="覆盖「今天」的日期（用于测试）")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()

    try:
        notes = Notes()
        folder_name, _ = notes.verify_folder()
    except NotesError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    rows = notes.list_notes()
    print(f"日志文件夹：「{folder_name}」共 {len(rows)} 条")
    print("═" * 56)

    plan: list[tuple[str, str, str]] = []   # (id, name, reason)
    keep: list[str] = []
    for nid, name in rows:
        reason = classify(name, today)
        if reason:
            plan.append((nid, name, reason))
        else:
            keep.append(name)

    print()
    print("【将删除】")
    if plan:
        for _, name, reason in plan:
            print(f"  · {name}")
            print(f"      {reason}")
    else:
        print("  （无）")

    print()
    print("【保留】")
    for name in keep:
        print(f"  · {name}")

    if not plan:
        print()
        print("✅ 无需清理。")
        return 0

    if not args.apply:
        print()
        print("═" * 56)
        print(f"【干跑】以上 {len(plan)} 条**尚未删除**。")
        print("确认无误后加 --apply 执行：")
        print(f"  python3 src/cleanup.py --apply")
        return 0

    print()
    print("【执行删除】")
    ok = 0
    for nid, name, _ in plan:
        try:
            if notes.delete(nid):
                print(f"  ✅ 已删除 {name}")
                ok += 1
            else:
                print(f"  ⚠️ 删除命令执行了，但复查时该条目仍在：{name}")
        except NotesError as e:
            print(f"  ❌ 删除失败 {name}：{e}", file=sys.stderr)

    print()
    after = notes.list_notes()
    print(f"结果：{len(rows)} 条 → {len(after)} 条（成功删除 {ok} 条）")
    print()
    print("剩余条目：")
    for _, name in after:
        print(f"  · {name}")
    print()
    print("（备忘录的删除是软删除，会在「最近删除」保留 30 天，属正常）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
