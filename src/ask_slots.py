#!/usr/bin/env python3
"""
用 Telegram 按钮逐条询问时段（分页选择器）。

解决的问题：
    "时段未指定"的条目需要你决定放哪里。若用文字回复（"1上午 2下午"），
    条目一多就既慢又容易写错。改成按钮后：一屏一条、点一下就定，
    且**只占一条消息**（点按钮是原地重绘，不刷屏）。

交互形态：
    ┌────────────────────────────┐
    │ 第 1/4 条 · 时段待指定       │
    │                            │
    │ 1215 305重新编号            │
    ├────────────────────────────┤
    │ [上午] [中午] [下午]         │
    │ [晚上] [明天] [不指定]       │
    │ [◀ 上一条] [下一条 ▶]        │
    │ [全部跳过]                  │
    └────────────────────────────┘

设计取舍（都以"实际能做到"为准）：
  · **一屏一条**而不是一屏多条：Telegram 的按钮是挂在整条消息上的，
    要让每题各有自己的按钮就得发多条消息。一屏一条既能"上面文本、下面按钮"，
    又能靠原地重绘保持界面干净。
  · **已选过的可以回来改**：点「◀ 上一条」回看，当前选项以 ● 标出。
  · **超时不回则走默认**（不指定时段），不阻塞其他流程 ——
    这与"agent 只在你需要时打扰你"的原则一致。
  · 「全部跳过」是必要的出口：否则你会被卡在一串提问里。

用法：
    python3 src/ask_slots.py --date 2026-10-02        # 问当天页里时段未指定的条目
    python3 src/ask_slots.py --date 2026-10-02 --dry  # 只打印将要问什么
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import parse as parser  # noqa: E402
import sync  # noqa: E402
import telegram as tg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 时段 → 回调里的短码。用英文缩写是为了省字节（callback_data 上限 64 字节）。
SLOT_CODES: list[tuple[str, str]] = [
    ("上午", "am"),
    ("中午", "noon"),
    ("下午", "pm"),
    ("晚上", "eve"),
    ("明天", "tmr"),
    ("不指定", "skip"),
]
CODE_TO_SLOT = {c: s for s, c in SLOT_CODES}


@dataclass
class Answer:
    line_no: int
    text: str
    date: str = ""               # 属于哪一天（支持跨天补齐）
    slot: str | None = None      # None = 还没定
    decided: bool = False        # 是否被显式选择过（含"不指定"）
    as_note: bool = False        # 你判定"这条不是待办" → 改成备忘


@dataclass
class Session:
    answers: list[Answer]
    index: int = 0
    message_id: int | None = None
    timed_out: bool = False
    skipped_all: bool = False
    verbose: bool = False
    confirming: bool = False     # 是否停在"提交确认"屏

    @property
    def undecided(self) -> list[Answer]:
        return [a for a in self.answers if not a.decided]

    @property
    def current(self) -> Answer:
        return self.answers[self.index]

    @property
    def decided_count(self) -> int:
        return sum(1 for a in self.answers if a.decided)


def _ack_text(data: str, sess: Session) -> str:
    """按钮点击后客户端顶部的小提示文字。"""
    if data == "n":
        return "下一条"
    if data == "p":
        return "上一条"
    if data == "d":
        return "完成"
    if data == "m":
        return "已标记为备忘"
    if data.startswith("s:"):
        parts = data.split(":")
        if len(parts) == 3:
            return f"已选：{CODE_TO_SLOT.get(parts[2], parts[2])}"
    return "已收到"


def confirm_text(sess: Session) -> str:
    """提交前的确认屏。"""
    lines = ["📋 请确认这次的选择：", ""]
    for i, a in enumerate(sess.answers, 1):
        if a.as_note:
            mark = "→ 备忘"
        elif a.decided:
            mark = f"→ {a.slot or '不指定'}"
        else:
            mark = "→ ⚠️ 未选择"
        lines.append(f"{i}. {a.text}  {mark}")

    if sess.undecided:
        lines.append("")
        lines.append(f"⚠️ 还有 {len(sess.undecided)} 条没选。")
        lines.append("点「⬅️ 回去补」继续，或点「就这样提交」按现状写入。")
    else:
        lines.append("")
        lines.append("全部已选，点「✅ 提交」写入。")
    return "\n".join(lines)


def confirm_keyboard(sess: Session) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    if sess.undecided:
        # 只回跳到第一条未选的，避免在确认屏和编辑屏之间来回绕
        first_undecided = sess.answers.index(sess.undecided[0])
        rows.append([("⬅️ 回去补", f"g:{first_undecided}")])
        rows.append([("就这样提交", "d")])
    else:
        rows.append([("✅ 提交", "d")])
        rows.append([("⬅️ 继续修改", "p")])
    return rows


def frame_text(sess: Session) -> str:
    """
    渲染当前这一屏的文字。

    刻意把「当前选择」写进正文（而不是只靠按钮上的 ●）：
    按钮上的标记在客户端上不够醒目，容易让人怀疑"我到底点上没有"。
    正文里有一行明确回显，点完立刻能看到结果。
    """
    a = sess.current
    total = len(sess.answers)
    done = sess.decided_count

    if a.as_note:
        chosen = "已改为「备忘」"
    elif a.decided:
        chosen = f"当前选择：{a.slot}"
    else:
        chosen = "尚未选择"

    # 已定的条目列表（一行一条，让你一眼看到进度）
    settled = [x for x in sess.answers if x.decided]
    if settled:
        brief = "、".join(
            f"{x.text[:8]}→{'备忘' if x.as_note else (x.slot or '不指定')}"
            for x in settled
        )
        settled_line = f"\n已定：{brief}\n"
    else:
        settled_line = ""

    return (
        f"第 {sess.index + 1}/{total} 条 · 时段待指定\n"
        f"（已定 {done}/{total}）\n"
        f"{settled_line}"
        f"\n"
        f"{a.text}\n"
        f"\n"
        f"{chosen}"
    )


def frame_keyboard(sess: Session) -> list[list[tuple[str, str]]]:
    """
    渲染按钮。当前已选的项前面加 ● 做标记。

    布局：3 列 × 2 行放六个时段、一行翻页、一行全局操作。
    """
    i = sess.index
    a = sess.current

    def label(name: str, code: str) -> str:
        chosen = a.decided and (
            (a.slot is None and code == "skip") or (a.slot == name and code != "skip")
        )
        return f"{'● ' if chosen else ''}{name}"

    # 2 行 × 3 列：比 3×2 更紧凑（少占一行高度），界面上更整齐
    slots = [[(label(n, c), f"s:{i}:{c}") for n, c in SLOT_CODES[r:r + 3]]
             for r in (0, 3)]

    nav: list[tuple[str, str]] = []
    if i > 0:
        nav.append(("◀ 上一条", "p"))
    # 只有"还有下一条"时才显示翻页按钮。
    #
    # 原先在最后一条会显示"✅ 完成"，但在**只有一条**的场景下
    # 前面还会有个孤零零的"下一条 ▶" —— 点了没反应（因为本来就没有下一条），
    # 用户会以为按钮坏了。而且用户在最后一条若因重绘延迟点到旧帧的"下一条"，
    # 也会出现"点了没反应"的错觉。
    if i < len(sess.answers) - 1:
        nav.append(("下一条 ▶", "n"))
    else:
        nav.append(("✅ 完成", "d"))

    slots.append(nav)
    # 用户反馈："全部跳过"使用率应该不高；更需要的是"这条其实不是待办"。
    # 后者能把误记成待办的条目纠正为备忘 —— 这是唯一需要人工判断的事，
    # 正好交回给用户（agent 不猜）。
    note_label = "● 这不是待办" if a.as_note else "这不是待办 → 改备忘"
    slots.append([(note_label, "m")])
    return slots


def render(sess: Session) -> None:
    """把当前屏画到 Telegram（首次发送，之后原地重绘）。"""
    if sess.confirming:
        text, kb = confirm_text(sess), confirm_keyboard(sess)
    else:
        text, kb = frame_text(sess), frame_keyboard(sess)

    if sess.message_id is None:
        r = tg.send_with_buttons(text, kb)
        sess.message_id = r.get("message_id")
        return

    # 重试一次：网络抖动导致的瞬时失败很常见，一次退避就能救回来。
    # 仍然失败才退化为发新消息（至少界面是对的）。
    for attempt in (1, 2):
        try:
            tg.edit_with_buttons(sess.message_id, text, kb)
            return
        except tg.TelegramError as e:
            if sess.verbose:
                print(f"    [重绘失败 {attempt}] {str(e)[:80]}", flush=True)
            time.sleep(0.6)
    r = tg.send_with_buttons(text, kb)
    sess.message_id = r.get("message_id")


def run_session(items: list[Answer], timeout_sec: int = 600,
                poll_interval: int = 2, verbose: bool = False) -> Session:
    """跑一轮问答。返回最终 Session（含每题的选择）。"""
    sess = Session(answers=items, verbose=verbose)
    render(sess)

    offset: int | None = None
    first_poll = True

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        # 关键：把 offset 在循环间续传。否则同一批 update 会被反复读到，
        # 表现为"第二次点击丢失"（第一次进确认屏、第二次提交时点不动）。
        cb = tg.wait_for_callback(timeout_sec=min(poll_interval * 2, 10),
                                  poll_interval=poll_interval,
                                  offset=offset, skip_history=first_poll)
        first_poll = False
        if cb is None:
            continue
        offset = cb.get("offset")

        # 逐个处理这一批点击，一个都不丢
        for one in (cb.get("batch") or [cb]):
            if not handle_click(sess, one):
                break
        else:
            continue
        break
    else:
        sess.timed_out = True

    # 收尾：**主动撤掉按钮**。
    # 不撤的话，会话结束后你再点那些按钮不会有任何响应（没有进程在听），
    # 客户端上按钮会一直转圈 —— 实测用户被这个坑过（点了 11 次没反应）。
    finish_ui(sess)
    return sess


def finish_ui(sess: Session) -> None:
    """把最后那条消息改成"已结束"的样子：去掉按钮、补一行结论。"""
    if sess.message_id is None:
        return
    try:
        tail = "✅ 已完成" if sess.decided_count == len(sess.answers) else \
               f"已结束（{sess.decided_count}/{len(sess.answers)} 条已选）"
        base = confirm_text(sess) if sess.confirming else frame_text(sess)
        token, chat = tg.load_config()
        tg._call(token, "editMessageText", {
            "chat_id": chat,
            "message_id": sess.message_id,
            "text": f"{base}\n\n{tail}",
        })
    except tg.TelegramError:
        # 收尾失败不影响结果：数据已经拿到，按钮顶多留一会儿。
        pass


def handle_click(sess: Session, cb: dict) -> bool:
    """
    处理一次按钮点击。返回 False 表示会话应当结束。

    抽成独立函数，是为了让"整批点击逐个处理"的循环读起来清楚 ——
    原来所有分支都堆在一个 while 里，加一层批处理就成了一团。
    """
    data = cb.get("data") or ""
    tg.answer_callback(cb.get("callback_id", ""), text=_ack_text(data, sess))

    if sess.verbose:
        print(f"    [按钮] data={data!r} index={sess.index} "
              f"decided={sess.decided_count}/{len(sess.answers)}", flush=True)

    if data == "n":
        if sess.index < len(sess.answers) - 1:
            sess.index += 1
        render(sess)
        return True

    if data == "p":
        if sess.index > 0:
            sess.index -= 1
        render(sess)
        return True

    if data == "d":
        if not sess.confirming:
            # 第一次点"完成"：不直接提交，先给确认屏 ——
            # 用户反馈过"点完成后只记录了一条"，根因是看不到还差几条。
            sess.confirming = True
            render(sess)
            return True
        return False   # 确认屏上再点一次才是真提交

    if data.startswith("g:"):
        try:
            sess.index = int(data.split(":", 1)[1])
        except ValueError:
            pass
        sess.confirming = False
        render(sess)
        return True

    if data == "m":
        a = sess.answers[sess.index]
        a.as_note = True
        a.slot = None
        a.decided = True
        if sess.index < len(sess.answers) - 1:
            sess.index += 1
        render(sess)
        return True

    if data.startswith("s:"):
        parts = data.split(":")
        if len(parts) == 3:
            try:
                idx = int(parts[1])
            except ValueError:
                return True
            code = parts[2]
            if 0 <= idx < len(sess.answers):
                a = sess.answers[idx]
                a.slot = CODE_TO_SLOT.get(code)
                a.as_note = False
                a.decided = True
                # 选完自动前进（用户确认要这个行为）；最后一条停原地等"完成"
                if idx == sess.index and idx < len(sess.answers) - 1:
                    sess.index = idx + 1
                render(sess)
        return True

    return True


def apply_answers(date_str: str, sess: Session) -> tuple[int, list[Path]]:
    """
    把选择写回留档 JSON 的 slot/kind 字段。

    支持跨天：按 Answer.date 分组写入（--all 模式下会一次问多天的条目）。

    注意：**不回写备忘录原文**。备忘录里那一行是你写的，机器改它会让
    "原文逐字保留"的保证失效；而 slot/kind 属于解析结果，
    落在留档里就够了，后续日报/顺延都读留档。
    """
    by_date: dict[str, dict[int, Answer]] = {}
    for a in sess.answers:
        if not a.decided:
            continue
        d = a.date or date_str
        by_date.setdefault(d, {})[a.line_no] = a

    changed_total = 0
    touched: list[Path] = []

    for d, line_map in by_date.items():
        p = sync.DAYS_DIR / f"{d}.json"
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        changed = 0
        for e in data.get("entries", []):
            a = line_map.get(e.get("line_no"))
            if a is None:
                continue

            if a.as_note:
                if e.get("kind") != "note":
                    e["kind"] = "note"
                    e["completed"] = None
                    e["slot"] = None
                    e["issues"] = ["由用户标记：这条不是待办"]
                    changed += 1
                continue

            if e.get("slot") != a.slot:
                e["slot"] = a.slot
                changed += 1
            if a.decided:
                issues = e.get("issues") or []
                e["issues"] = [x for x in issues if "时段未指定" not in x]

        if changed:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            changed_total += changed
            touched.append(p)

    return changed_total, touched


def collect_pending(dates: list[str]) -> list[Answer]:
    """从若干天的留档里收集「未完成且时段未指定」的条目。"""
    out: list[Answer] = []
    for d in dates:
        result = sync.load_archive(d)
        if result is None:
            continue
        for e in result.todos:
            if e.completed is not True and e.slot is None:
                out.append(Answer(line_no=e.line_no, text=e.text, date=d))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="用按钮询问时段")
    ap.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--all", action="store_true",
                    help="补齐所有历史留档里待指定时段的条目（跨天一起问）")
    ap.add_argument("--dry", action="store_true", help="只本地预览界面，不发送")
    ap.add_argument("--timeout", type=int, default=900, help="等待总时长（秒）")
    ap.add_argument("--verbose", action="store_true",
                    help="打印每次按钮回调（排查「点了没反应」用）")
    args = ap.parse_args()

    if args.all:
        dates = sorted(p.stem for p in sync.DAYS_DIR.glob("*.json"))
        items = collect_pending(dates)
        scope = f"全部留档（{len(dates)} 天）"
    else:
        date_str = args.date or dt.date.today().isoformat()
        try:
            dt.date.fromisoformat(date_str)
        except ValueError:
            print(f"❌ 日期格式不对：{date_str}", file=sys.stderr)
            return 1
        items = collect_pending([date_str])
        scope = date_str

    if not items:
        print(f"✅ {scope}：没有「时段未指定」的条目，无需询问")
        return 0

    print(f"待询问 {len(items)} 条（{scope}）：")
    for i, a in enumerate(items, 1):
        prefix = f"[{a.date}] " if args.all else ""
        print(f"  {i}. {prefix}{a.text}")

    if args.dry:
        print()
        sess = Session(answers=items)
        for i in range(len(items)):
            sess.index = i
            print(f"── 第 {i + 1} 屏 ──")
            for line in frame_text(sess).splitlines():
                print("  │", line)
            print("  ├" + "─" * 30)
            for row in frame_keyboard(sess):
                print("  │ " + "   ".join(f"[{lbl}]" for lbl, _ in row))
            print()
        print("（--dry：未发送，也未写入）")
        return 0

    print()
    print("已在 Telegram 发出按钮，等你选择…")
    sess = run_session(items, timeout_sec=args.timeout, verbose=args.verbose)

    print()
    if sess.timed_out:
        print(f"⏱️ 超时（未完成全部）。已确定 {sess.decided_count}/{len(items)} 条，"
              f"这部分仍会写入。")

    print("你的选择：")
    for a in sess.answers:
        if a.as_note:
            mark = "备忘（不是待办）"
        elif a.decided:
            mark = a.slot or "不指定"
        else:
            mark = "（未定，保持原样）"
        prefix = f"[{a.date}] " if args.all else ""
        print(f"  · {prefix}{a.text}  →  {mark}")
    if sess.undecided:
        print(f"  ⚠️ 有 {len(sess.undecided)} 条未选择，保持原样（时段仍未指定）")

    changed, touched = apply_answers("", sess)
    print()
    if changed:
        print(f"✅ 已写入留档 {changed} 条：")
        for p in touched:
            try:
                print(f"   {p.relative_to(ROOT)}")
            except ValueError:
                print(f"   {p}")
    else:
        print("（没有变化，未写文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
