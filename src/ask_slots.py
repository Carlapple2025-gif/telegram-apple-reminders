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


@dataclass
class Session:
    answers: list[Answer]
    index: int = 0
    message_id: int | None = None
    timed_out: bool = False
    skipped_all: bool = False

    @property
    def current(self) -> Answer:
        return self.answers[self.index]

    @property
    def decided_count(self) -> int:
        return sum(1 for a in self.answers if a.decided)


def frame_text(sess: Session) -> str:
    """渲染当前这一屏的文字。"""
    a = sess.current
    total = len(sess.answers)
    done = sess.decided_count
    slot_line = f"当前选择：{a.slot}" if a.decided else "尚未选择"
    return (
        f"第 {sess.index + 1}/{total} 条 · 时段待指定\n"
        f"（已定 {done}/{total}）\n"
        f"\n"
        f"{a.text}\n"
        f"\n"
        f"{slot_line}"
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
    slots.append([("⏭ 全部跳过", "x")])
    return slots


def render(sess: Session) -> None:
    """把当前屏画到 Telegram（首次发送，之后原地重绘）。"""
    if sess.message_id is None:
        r = tg.send_with_buttons(frame_text(sess), frame_keyboard(sess))
        sess.message_id = r.get("message_id")
    else:
        try:
            tg.edit_with_buttons(sess.message_id, frame_text(sess),
                                 frame_keyboard(sess))
        except tg.TelegramError:
            # 重绘失败（少数情况下 Telegram 会拒绝无变化的编辑）时，
            # 退化为发新消息，至少界面是对的。
            r = tg.send_with_buttons(frame_text(sess), frame_keyboard(sess))
            sess.message_id = r.get("message_id")


def run_session(items: list[Answer], timeout_sec: int = 600,
                poll_interval: int = 2) -> Session:
    """跑一轮问答。返回最终 Session（含每题的选择）。"""
    sess = Session(answers=items)
    render(sess)

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        cb = tg.wait_for_callback(timeout_sec=min(poll_interval * 2, 10),
                                  poll_interval=poll_interval)
        if cb is None:
            continue

        data = cb["data"]
        tg.answer_callback(cb["callback_id"])

        if data == "x":
            sess.skipped_all = True
            break

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
            break

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
        new_slot = a.slot
        if e.get("slot") != new_slot:
            e["slot"] = new_slot
            changed += 1
            issues = e.get("issues") or []
            e["issues"] = [x for x in issues if "时段未指定" not in x]

    if changed:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed, p


def main() -> int:
    ap = argparse.ArgumentParser(description="用按钮询问时段")
    ap.add_argument("--date", help="日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--dry", action="store_true", help="只打印将要问什么，不发送")
    ap.add_argument("--timeout", type=int, default=600, help="等待总时长（秒）")
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
    sess = run_session(items, timeout_sec=args.timeout)

    print()
    if sess.skipped_all:
        print("你选择了「全部跳过」——不写入任何时段。")
        return 0
    if sess.timed_out:
        print(f"⏱️ 超时（未完成全部）。已确定 {sess.decided_count}/{len(items)} 条，"
              f"这部分仍会写入。")

    print("你的选择：")
    for a in sess.answers:
        mark = a.slot if a.decided else "（未定）"
        print(f"  · {a.text}  →  {mark}")

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
