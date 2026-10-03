#!/usr/bin/env python3
"""
21:30 日报：汇总当天进度，推送提醒你补打钩。

为什么这条推送必须存在：
  顺延依赖「完成状态」准确。而完成状态的权威是**提醒事项的 completed** ——
  只有你在那里打了钩，程序才知道哪些没做完。如果没人提醒，你很可能忘；
  忘了打钩，做完的事就会被顺延到明天。所以这条推送不是"催办"，
  它是**整条链路的数据质量保障**。

完成状态的来源（重要）：
  权威 = 提醒事项的 completed；备忘录里的 [x] 视为等价表达。
  两者合并时**倾向"完成"**（详见 src/completion.py）。
  提醒事项不可用时退化为只读备忘录标记，并在日报里**如实标注** ——
  绝不能让人误以为"这就是全部事实"。

措辞原则：不催、不评判。你打钩的动力来自"看到自己完成了什么"，
不是来自"被机器指责还剩几件"。所以完成项放在前面。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import NotesError  # noqa: E402
import completion  # noqa: E402
import notify  # noqa: E402
import parse as parser  # noqa: E402
import sync  # noqa: E402


def build_report(date_str: str, resolved: completion.Resolved,
                 reminders_list: str) -> tuple[str, str]:
    """生成 (标题, 正文)。"""
    todos = resolved.todos
    done = resolved.done
    open_items = resolved.open
    total = len(todos)

    title = f"📋 {date_str} 今日复盘"

    if total == 0:
        return title, "今天没有登记待办。\n\n如果只想记点东西，用 * 开头写备忘。"

    lines: list[str] = []

    # 完成项放前面：让人先看到成果
    if done:
        lines.append(f"✅ 已完成 {len(done)}/{total}")
        for r in done:
            slot = f"　{r.entry.slot} " if r.entry.slot else "　"
            lines.append(f"{slot}{r.entry.text}")
        lines.append("")

    if open_items:
        lines.append(f"⏳ 未完成 {len(open_items)} 件")
        for r in open_items:
            slot = f"　{r.entry.slot} " if r.entry.slot else "　"
            lines.append(f"{slot}{r.entry.text}")
        lines.append("")
        lines.append(f"做完的请在「提醒事项 → {reminders_list}」里打钩 ✓")
        lines.append("没打钩的明早会顺延到次日页")
    else:
        lines.append("🎉 今天的待办全部完成")

    notes_count = len([e for e in resolved.entries if e.entry.kind == "note"])
    if notes_count:
        lines.append("")
        lines.append(f"（另有 {notes_count} 条备忘）")

    # 如实标注异常情况 —— 否则你会把"数据不全"误当成"事实如此"
    if not resolved.reminders_available:
        lines.append("")
        lines.append(f"⚠️ 读取提醒事项失败，上面只依据备忘录标记")
    if resolved.not_pushed:
        lines.append("")
        lines.append(f"⚠️ 有 {len(resolved.not_pushed)} 条还没同步到提醒事项"
                     f"（可能是当天页建得比 08:00 晚）")

    return title, "\n".join(lines)


def note_url(note_id: str) -> str | None:
    """
    点击直达备忘录的链接。

    格式参考 x-coredata id：applenotes://showNote?identifier=<id>
    这是**未经验证**的猜测 —— 所以做成可选增强：点开无效也不影响日报本身。
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
    ap.add_argument("--sync", action="store_true",
                    help="推送前先把待办同步到提醒事项（保证状态查得到）")
    ap.add_argument("--ask", action="store_true",
                    help="推送后，若有「时段未指定」的条目，用 Telegram 按钮询问")
    ap.add_argument("--channels", default="telegram,bark",
                    help="推送通道，逗号分隔（默认 telegram,bark —— 互为冗余）")
    args = ap.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{date_str}", file=sys.stderr)
        return 1

    # ── 可选：先同步到提醒事项
    if args.sync:
        import push_tasks
        print("（先同步待办到提醒事项，保证完成状态可查）")
        rc = push_tasks.sync_day(date_str, apply=True, refresh=True, quiet=True)
        if rc != 0:
            print(f"⚠️ 同步返回 {rc}，继续生成日报", file=sys.stderr)

    # ── 取数据
    if args.refresh:
        try:
            s = sync.sync_day(date_str)
        except NotesError as e:
            print(f"❌ 读取备忘录失败：{e}", file=sys.stderr)
            return 2
        if s is None:
            print(f"⚠️  {date_str} 没有当天页")
            return 1
        result, note_id = s.result, s.note_id
    else:
        result = sync.load_archive(date_str)
        if result is None:
            print(f"没有 {date_str} 的留档。加 --refresh 从备忘录读取，"
                  f"或先跑 src/read_day.py {date_str}", file=sys.stderr)
            return 1
        note_id = sync.archive_meta(date_str).get("note_id", "")

    # ── 合并完成状态（权威 = 提醒事项）
    resolved = completion.resolve(result, note_id)

    # 回填并**持久化**到留档 —— 顺延在次日早上读的就是它。
    # 不落盘的话，你今晚打的钩会失效，做完的事会被顺延到明天。
    changed = completion.write_back(result, resolved)
    if changed:
        sync.save_archive(date_str, result)

    try:
        from reminders import Reminders
        reminders_list = Reminders().list_name
    except Exception:  # noqa: BLE001
        reminders_list = "PDCA"

    title, body = build_report(date_str, resolved, reminders_list)

    print(f"标题：{title}")
    print("─" * 46)
    print(body)
    print("─" * 46)

    if not resolved.reminders_available:
        print(f"⚠️  提醒事项不可用：{resolved.reminders_error}")
    if changed:
        print(f"（已回填 {changed} 条完成状态并写入留档）")
    if resolved.not_pushed:
        print(f"（{len(resolved.not_pushed)} 条尚未同步到提醒事项，"
              f"本次按备忘录标记判断）")

    if args.no_push:
        return 0

    url = note_url(note_id)
    results = notify.broadcast(
        title, body,
        channels=[c.strip() for c in args.channels.split(",") if c.strip()],
        bark_url=url,
    )
    print()
    for ch, ok, msg in results:
        print(f"  {'✅' if ok else '❌'} {ch}: {msg}")

    # 至少一个通道成功就算推送完成 —— 两个通道互为冗余，
    # 全挂才算失败（那时你会从日志里发现）。
    ok_any = any(ok for _, ok, _ in results)

    # ── 可选：用按钮询问「时段未指定」的条目
    if args.ask:
        pending = [r for r in resolved.open if not r.entry.slot]
        if pending:
            print()
            print(f"有 {len(pending)} 条时段未指定 → 发按钮询问")
            try:
                import ask_slots
                items = [ask_slots.Answer(line_no=r.entry.line_no,
                                          text=r.entry.text, date=date_str)
                         for r in pending]
                sess = ask_slots.run_session(items, timeout_sec=900)
                changed, touched = ask_slots.apply_answers(date_str, sess)
                if changed:
                    print(f"✅ 你的选择已写入留档（{changed} 条）")
                    for p2 in touched:
                        print(f"   {p2.name}")
                elif sess.decided_count == 0:
                    print("（你未做选择，时段保持未指定）")
            except Exception as e:  # noqa: BLE001
                # 询问失败不该影响日报已送达的事实
                print(f"⚠️ 询问环节出错：{e}", file=sys.stderr)
        else:
            print()
            print("（所有待办都已有时段，无需询问）")

    return 0 if ok_any else 3


if __name__ == "__main__":
    sys.exit(main())
