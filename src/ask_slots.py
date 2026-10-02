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

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cb = tg.wait_for_callback(timeout_sec=min(poll_interval * 2, 10),
                                  poll_interval=poll_interval)
        if cb is None:
            continue

        data = cb["data"]
        # 即时反馈：客户端顶部会弹一小行。这是"点没点上"最直接的证据 ——
        # 没有它，用户只能盯着按钮变化猜，网络慢时就显得像没反应。
        tg.answer_callback(cb["callback_id"], text=_ack_text(data, sess))

        if sess.verbose:
            print(f"    [按钮] data={data!r} index={sess.index} "
                  f"decided={sess.decided_count}/{len(sess.answers)}", flush=True)

        if data == "m":
            a = sess.answers[sess.index]
            a.as_note = True
            a.slot = None
            a.decided = True
            if sess.index < len(sess.answers) - 1:
                sess.index += 1
            render(sess)
            continue

        if data == "n":
            if sess.index < len(sess.answers) - 1:
                sess.index += 1
            render(sess)
            continue

        if data == "p":
            if sess.index > 0:
                sess.index -= 1
            render(sess)
            continue

        if data == "d":
            if not sess.confirming:
                # 第一次点"完成"：不直接提交，先给确认屏 ——
                # 用户反馈过"点完成后只记录了一条"，根因是看不到还差几条。
                # 把差额摆明，比什么都强。
                sess.confirming = True
                render(sess)
                continue
            break

        if data.startswith("g:"):
            # 从确认屏回跳到指定条目
            try:
                sess.index = int(data.split(":", 1)[1])
            except ValueError:
                pass
            sess.confirming = False
            render(sess)
            continue

        if data.startswith("s:"):
            parts = data.split(":")
            if len(parts) == 3:
                try:
                    idx = int(parts[1])
                except ValueError:
                    continue
                code = parts[2]
                if 0 <= idx < len(sess.answers):
                    slot = CODE_TO_SLOT.get(code)
                    a = sess.answers[idx]
                    a.slot = slot
                    a.decided = True
                    # 选完自动前进一条，省一次点击；最后一条则停留在原地（等"完成"）
                    if idx == sess.index and idx < len(sess.answers) - 1:
                        sess.index = idx + 1
                    render(sess)
            continue

    else:
        sess.timed_out = True

    return sess


def apply_answers(date_str: str, sess: Session) -> tuple[int, Path | None]:
    """
    把选择写回留档 JSON 的条目 slot 字段。

    注意：**不回写备忘录原文**。理由：备忘录里那一行是你写的，
    机器改它会让"原文逐字保留"的保证失效；而 slot 属于解析结果，
    落在留档里就够了，后续日报/顺延都读留档。
    """
    p = sync.DAYS_DIR / f"{date_str}.json"
    if not p.is_file():
        return 0, None
    data = json.loads(p.read_text(encoding="utf-8"))
    by_line = {a.line_no: a for a in sess.answers if a.decided}

    changed = 0
    for e in data.get("entries", []):
        a = by_line.get(e.get("line_no"))
        if a is None:
            continue

        if a.as_note:
            # 你判定"这不是待办" → 改成备忘。
            # 只改留档里的解析结果，**不动备忘录原文**（原文逐字保留是核对的基础）。
            if e.get("kind") != "note":
                e["kind"] = "note"
                e["completed"] = None
                e["slot"] = None
                e["issues"] = ["由用户标记：这条不是待办"]
                changed += 1
            continue

        new_slot = a.slot
        if e.get("slot") != new_slot:
            e["slot"] = new_slot
            changed += 1
            issues = e.get("issues") or []
            # 显式选了"不指定"也算已决定，把提示去掉；
            # 只有真的没选（decided=False）才保留提示。
            if a.decided:
                e["issues"] = [x for x in issues if "时段未指定" not in x]

    if changed:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed, p


def main() -> int:
    ap = argparse.ArgumentParser(description="用按钮询问时段")
    ap.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--dry", action="store_true", help="只打印将要问什么，不发送")
    ap.add_argument("--timeout", type=int, default=600, help="等待总时长（秒）")
    ap.add_argument("--verbose", action="store_true",
                    help="打印每次按钮回调（排查「点了没反应」用）")
    args = ap.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date_str)
    except ValueError:
        print(f"❌ 日期格式不对：{date_str}", file=sys.stderr)
        return 1

    result = sync.load_archive(date_str)
    if result is None:
        print(f"没有 {date_str} 的留档", file=sys.stderr)
        return 1

    pending = [e for e in result.todos if e.slot is None]
    if not pending:
        print(f"✅ {date_str} 没有「时段未指定」的条目，无需询问")
        return 0

    items = [Answer(line_no=e.line_no, text=e.text) for e in pending]

    print(f"待询问 {len(items)} 条：")
    for i, a in enumerate(items, 1):
        print(f"  {i}. {a.text}")

    if args.dry:
        # 本地预览界面：把每一屏都画出来，不发 Telegram。
        # 这样"界面长什么样"可以直接在这里看，不必先发出去再撤回。
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
    if sess.skipped_all:
        print("你选择了「全部跳过」——不写入任何时段。")
        return 0
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
        print(f"  · {a.text}  →  {mark}")
    if sess.undecided:
        print(f"  ⚠️ 有 {len(sess.undecided)} 条未选择，保持原样（时段仍未指定）")

    changed, p = apply_answers(date_str, sess)
    if changed:
        print()
        print(f"✅ 已写入留档 {changed} 条：{p.relative_to(ROOT)}")
    else:
        print()
        print("（没有变化，未写文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
