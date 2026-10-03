#!/usr/bin/env python3
"""
顺延：把某天未完成的事项追加到次日页。

这是**第一个会写你备忘录的功能**，所以规矩更严：

  1. 默认干跑（dry-run）—— 不写任何东西，只打印"我打算写什么"
  2. 真正写入前必须显式加 --apply
  3. 写入后读回验证（备忘录有"不报错但没生效"的静默失败）
  4. 幂等 —— 重复跑不会写重复内容

几个刻意的设计决定（都有理由）：

**只顺延"未完成待办"，不包括备忘。** 备忘是资料不是任务，跟着日期漂移没有意义。

**原文照抄，行尾加 ⟳，绝不改写。** 改写会让下一次去重失效，也会让你认不出
这条是从昨天带过来的。

**不带复选框标记。** 在新页面里它又变成"未完成"，写 `- [x]` 会自相矛盾。
统一写成 `- [ ] 内容 ⟳`。

**只在当天页不存在时才新建。** 已存在就追加 —— 避免覆盖你当天已经写的内容。

**已完成的项不会被顺延。**

完成状态从哪来（重要）：
    权威是**提醒事项的 `completed`**，不是备忘录里的 `[x]`。
    流程上：21:30 日报会 `completion.resolve()` 出权威状态、回填进留档 JSON，
    而本脚本次日 07:00 读的就是那份留档 —— 所以顺延拿到的是你昨晚打完钩之后的
    真实状态。

    如果 21:30 那次没跑成（或你想立刻用最新状态），加 `--resolve` 现场重新
    从提醒事项解析一次。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError  # noqa: E402
import parse as parser  # noqa: E402
import sync  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAYS_DIR = ROOT / "data" / "days"

# 顺延标记。用 ⟳（U+27F3）而不是 "<<" 之类：
#   · 视觉上和"这条是转过来的"直觉一致
#   · 不太可能在正文里自然出现，去重判断可靠
CARRY_MARK = "⟳"


def html_escape_free(line: str) -> str:
    """清理将要写入备忘录的一行：去掉换行、压缩多余空白。"""
    line = line.replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]+", " ", line).strip()


def content_fingerprint(text: str) -> str:
    """
    内容指纹：用于判断"两行是不是同一件事"。

    把**所有装饰性标记**剥掉后归一化：
      · 顺延标记 ⟳（可能不止一个 —— 曾经产生过 `⟳ ⟳`）
      · 复选框 `[ ]` / `[x]`
      · 行首列表符号 `-` `*` `•`
      · 时段前缀 `@上午` 等

    为什么必须这么彻底（两次实测教训）：
      ① 只按原样比较 → 「甲 ⟳」≠「甲」→ 同一条被顺延第二遍
      ② 只剥 ⟳ 不剥复选框 → 「- [ ] 甲」≠「甲」时又漏
    所以统一成一个函数，所有需要"判断是不是同一条"的地方都调它。
    """
    t = text
    # 顺延标记可能带来源日期、也可能出现多个（历史遗留），
    # 必须**连同日期一起**剥掉 —— 只剥 ⟳ 会留下 "10-02"，
    # 于是「甲 ⟳10-02」的指纹变成 "甲 10-02"，与「甲」对不上（实测踩到）。
    t = re.sub(rf"{CARRY_MARK}\s*\d{{2}}-\d{{2}}", " ", t)
    t = t.replace(CARRY_MARK, " ")
    # 剥掉行首列表符号与复选框
    body, _, _ = parser.strip_leading_marker(t)
    t = body or t
    return parser.normalize_line(t)


# 兼容旧名（selftest 里引用过）
dedup_key = content_fingerprint


def carried_before(line: str, target_date: str, src_date: str = "") -> bool:
    """
    这一行是不是"已经被顺延过一次"的？

    判据（宽严并济）：
      · 行里有 ⟳ 且**没有**日期 → 无法证明是本次写的 → 视为历史遗留，排除。
        宁可漏带一条（你会看到并手动补），也不要重复带（会积累成一串 ⟳）。
      · 行里有 ⟳MM-DD：等于 src_date 的月日 → 是本次要写的；其余 → 历史遗留。
    """
    if CARRY_MARK not in line:
        return False
    m = re.search(rf"{CARRY_MARK}\s*(\d{{2}}-\d{{2}})", line)
    if not m:
        return True                      # 裸 ⟳ → 当作历史遗留
    if not src_date:
        return True
    return m.group(1) != src_date[5:]   # 日期不是"本次来源" → 历史遗留


def carry_text(entry: parser.Entry, from_date: str = "") -> str:
    """
    把一条待办转成顺延到次日页的那一行。

    标记写成 `⟳10-02`（带来源日期）而不是裸 `⟳`：
    裸标记虽然好看，但**无法区分"这条是从更早带过来的"**，
    于是它会在下一次顺延时又被带走一次 —— 形成链式顺延
    （实测发生过：10-02 的条目被带到 10-03，然后又被带到 10-04）。
    带上来源日期后，就能判断"这条已经带过一次了，别再带"。
    """
    base = html_escape_free(entry.text)
    tag = f"{CARRY_MARK}{from_date[5:]}" if from_date else CARRY_MARK
    return f"- [ ] {base} {tag}"


def build_plan(prev: parser.ParseResult, next_text: str | None,
               src_date: str, target_date: str) -> tuple[list[str], list[str]]:
    """
    计算要顺延的行。返回 (要追加的行, 跳过原因说明)。

    幂等与去重都在这层做：
      · 次日页已有同样内容 → 跳过（避免重复）
      · 次日页已带 ⟳ 的旧顺延项 → 视为已处理
      · 相似度高的也提示，但不静默丢（交给人看）
    """
    existing_lines: list[str] = []
    existing_norm: list[str] = []
    if next_text:
        for raw in next_text.replace("\r\n", "\n").split("\n"):
            if not raw.strip():
                continue
            existing_lines.append(raw)
            existing_norm.append(content_fingerprint(raw))

    plan: list[str] = []
    skipped: list[str] = []

    # 目标页里已经存在的"历史顺延项" —— 用来判断哪些条目已被带过一次
    already_carried = {
        content_fingerprint(l) for l in existing_lines
        if carried_before(l, target_date, src_date)
    }

    for entry in prev.open_todos:
        text = html_escape_free(entry.text)
        if not text:
            skipped.append(f"第 {entry.line_no} 行内容为空，跳过")
            continue

        norm = content_fingerprint(text)

        # 防链式顺延（两条判据，缺一不可）：
        #
        # 判据 A：**来源条目自己就带旧 ⟳ 标记** —— 说明它在更早的某天
        #   已经被顺延过一次了。这条是主力：实测 10-02 的条目被带到 10-03 后，
        #   10-03 的条目仍带 ⟳10-02，再跑就会继续往 10-04 传。
        # 判据 B：目标页里已有同内容的 ⟳ 项 —— 覆盖"重复运行"的场景
        #   （同一天跑两次，第一次已写入、第二次不该再写）。
        #
        # 两个判据分别对应两种不同的重复来源，只留一个都会漏。
        if carried_before(entry.raw, target_date, src_date):
            skipped.append(f"「{text}」在更早的某天已顺延过，不再往后传")
            continue
        if norm in already_carried:
            skipped.append(f"「{text}」目标页已有 ⟳ 项，不再重复带")
            continue

        # 完全一致 → 已经有这条了
        if norm in existing_norm:
            skipped.append(f"「{text}」次日页已有同样内容，跳过")
            continue

        # 高度相似 → 大概率是同一件事，但不自动判定，列给人看
        similar_hits = [(e, parser.similar(norm, e)) for e in existing_norm]
        close = [e for e, r in similar_hits if r >= 0.85]
        if close:
            skipped.append(
                f"「{text}」与次日页已有内容高度相似（{close[0]}），跳过以免重复 —— "
                f"若确实是两件事，请手工补写"
            )
            continue

        plan.append(carry_text(entry, src_date))

    return plan, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="把未完成事项顺延到次日页")
    ap.add_argument("date", nargs="?", help="来源日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--apply", action="store_true",
                    help="真正写入备忘录（不加则只干跑预览）")
    ap.add_argument("--next", help="覆盖次日日期（默认来源日期 +1）")
    ap.add_argument("--offline", action="store_true",
                    help="不查备忘录，假定次日页不存在（用于离线验证计划生成）")
    ap.add_argument("--resolve", action="store_true",
                    help="现场从提醒事项重新解析完成状态（默认用留档里的回填结果）")
    args = ap.parse_args()

    src_date_str = args.date or dt.date.today().isoformat()
    try:
        src_date = dt.date.fromisoformat(src_date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{src_date_str}", file=sys.stderr)
        return 1

    next_date = dt.date.fromisoformat(args.next) if args.next else src_date + dt.timedelta(days=1)
    next_date_str = next_date.isoformat()

    print(f"顺延：{src_date_str} → {next_date_str}")
    print("═" * 56)

    # ── 取来源
    prev = sync.load_archive(src_date_str)
    source_desc = "留档 JSON"
    if prev is None:
        print(f"（没有 {src_date_str} 的留档，改为直接读备忘录）")
        try:
            notes = Notes()
            note = notes.find_daily_page(src_date_str)
        except NotesError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        if note is None:
            print(f"❌ 备忘录里也没有 {src_date_str} 的当天页", file=sys.stderr)
            return 1
        prev = parser.parse(note.plaintext)
        source_desc = "备忘录"

    # ── 完成状态：权威是提醒事项
    # 默认直接用留档里的 completed（21:30 日报已回填过，含你打完钩的状态）。
    # --resolve 时现场再解析一次，用于"日报没跑成"或"想立刻用最新状态"。
    if args.resolve or source_desc == "备忘录":
        import completion
        resolved = completion.resolve(prev, sync.archive_meta(src_date_str).get("note_id", ""))
        changed = completion.write_back(prev, resolved)
        if resolved.reminders_available:
            if changed:
                sync.save_archive(src_date_str, prev)
            print(f"完成状态：已从提醒事项解析（修正 {changed} 条）")
        else:
            print(f"⚠️  提醒事项不可用（{resolved.reminders_error}），"
                  f"退回用备忘录标记判断")
    else:
        print("完成状态：取自留档（21:30 日报已回填）")

    print(f"来源：{source_desc}（{src_date_str}）")
    print(f"  待办 {len(prev.todos)} 条，其中未完成 {len(prev.open_todos)} 条")

    if not prev.open_todos:
        print()
        print("✅ 没有未完成事项，无需顺延。")
        return 0

    # ── 取目标
    notes = None
    target = None
    if args.offline:
        print("  次日页：（--offline，假定不存在）")
        next_text = None
    else:
        try:
            notes = Notes()
            target = notes.find_daily_page(next_date_str)
        except NotesError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2
        if target is None:
            print(f"  次日页：不存在，将新建 {next_date_str}")
            next_text = None
        else:
            print(f"  次日页：已存在（{target.name}），将追加")
            next_text = target.plaintext

    # ── 计算计划
    plan, skipped = build_plan(prev, next_text,
                               target_date=next_date_str, src_date=src_date_str)

    print()
    print("── 计划写入的内容 " + "─" * 40)
    if plan:
        for line in plan:
            print(f"  + {line}")
    else:
        print("  （无需写入 —— 所有未完成项次日页都已有）")

    if skipped:
        print()
        print("── 跳过 " + "─" * 48)
        for s in skipped:
            print(f"  · {s}")

    if not plan:
        print()
        print("✅ 无事可做。")
        return 0

    # ── 干跑到此为止
    if not args.apply:
        print()
        print("═" * 56)
        print("【干跑】以上内容**尚未写入**备忘录。")
        print()
        print("确认无误后，加上 --apply 真正执行：")
        print(f"  python3 src/carry_over.py {src_date_str} --apply")
        return 0

    # ── 真正写入
    if args.offline:
        print()
        print("⚠️  --offline 模式下不能写入（没连接备忘录）。去掉 --offline 再执行。",
              file=sys.stderr)
        return 1
    print()
    print("── 写入 " + "─" * 48)
    try:
        if target is None:
            body_lines = [next_date_str] + plan
            note = notes.create(body_lines)
            print(f"  ✅ 已新建次日页「{note.name}」并写入 {len(plan)} 行")
        else:
            notes.append_lines(target.id, plan)
            print(f"  ✅ 已向「{target.name}」追加 {len(plan)} 行")

        # 读回验证：不只看有没有报错
        fresh = notes.find_daily_page(next_date_str)
        if fresh is None:
            print("  ❌ 写入后读回找不到次日页 —— 写入未生效", file=sys.stderr)
            return 3

        # 逐条确认正文真的出现了。用"剥掉标记后的正文"比对，
        # 因为备忘录会把 HTML 规范化，不能直接比整行字符串。
        missing = []
        for line in plan:
            probe = line.split("]", 1)[-1]
            probe = probe.replace(CARRY_MARK, "").strip()
            if probe and probe not in fresh.plaintext:
                missing.append(probe)
        if missing:
            print("  ⚠️ 部分内容读回不可见，请手工核对：", file=sys.stderr)
            for m in missing:
                print(f"      {m}", file=sys.stderr)
            return 3
        print(f"  ✅ 读回验证通过（{len(plan)} 条全部可见）")
        # 顺延刚建/改了次日页 → 立刻为它建一份留档。
        #
        # 为什么必须做：顺延只写备忘录，而留档是后续所有步骤的输入
        # （日报读它、时段询问扫描它、再下次顺延读它）。不补这一步，
        # 新的一页就**不在系统视野内** —— 实测踩到：10-03 建好后留档里
        # 没有它，`ask_slots --all` 因此看不到它上面的条目。
        try:
            s2 = sync.sync_day(next_date_str)
            if s2 is not None:
                print(f"  ✅ 已为 {next_date_str} 建立留档"
                      f"（待办 {len(s2.result.todos)} 条）")
            else:
                print(f"  ⚠️ 顺延已写入，但读回时找不到 {next_date_str}",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            # 留档失败不该让顺延本身算失败：备忘录已经写进去了。
            print(f"  ⚠️ 建立留档失败：{e}", file=sys.stderr)
    except NotesError as e:
        print(f"  ❌ 写入失败：{e}", file=sys.stderr)
        return 3

    print()
    print("完成。建议顺手核对一下：")
    print(f"  备忘录 → logs → {next_date_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
