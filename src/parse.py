#!/usr/bin/env python3
"""
解析当天页的纯文本，产出结构化条目。

设计原则（都是前面探测得出的）：
  · 只做「整理」，不做「判断」 —— 类型来自用户写的标记，解析器不猜
  · 原文逐字保留 —— 便于人工核对，也让去重不会因为改写而失效
  · 无法识别的行不丢弃 —— 归为「待定」交给上层询问，绝不静默丢掉

标记约定：
  - [ ] xxx   待办，未完成
  - [x] xxx   待办，已完成
  * xxx       备忘（不进待办）
  @上午|中午|下午|晚上|明天 xxx   待办 + 时段
  其它非空行   待办，时段未指定
  # 开头       元信息，跳过
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher

# ── 标记定义

# 时段标签。放在最前面以便用户书写，解析时剥掉。
SLOTS = ["上午", "中午", "下午", "晚上", "明天"]

# 这些字符在不同输入法/智能标点下会互相替换，统一归一化。
# 例：iOS 智能标点会把 "- " 变成 "– " 或 "— "，如果把这种行当成普通待办，
# 用户会以为标记生效了、实际没有 —— 那是静默错误，必须容忍。
DASH_CHARS = "-–—−‐"
BULLET_CHARS = "*•·・"

# 已完成 / 未完成的复选框。半角全角都接受。
CHECKED = re.compile(r"^\[[xX✓✔]\]")
UNCHECKED = re.compile(r"^\[\s*\]")

# 日期标题：2026-10-02 / 2026/10/02 / 2026.10.02
DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def normalize_line(s: str) -> str:
    """归一化用于比较与去重的形式（不改动原文）。"""
    s = unicodedata.normalize("NFKC", s)
    s = s.strip()
    # 折叠空白
    s = re.sub(r"\s+", " ", s)
    return s


def strip_leading_marker(line: str) -> tuple[str, str | None, str | None]:
    """
    剥掉行首标记，返回 (剩余内容, 类型, 时段)。

    类型：'todo' | 'note' | None（没识别到标记）
    时段：'上午' / '中午' / ... 或 None
    """
    s = line.strip()
    if not s:
        return "", None, None

    kind = None
    completed = None

    # 行首的列表符号（- * • 等）。归一化后再判断。
    if s and (s[0] in DASH_CHARS or s[0] in BULLET_CHARS):
        marker = s[0]
        rest = s[1:].lstrip()
        if marker in BULLET_CHARS:
            kind = "note"
        else:
            kind = "todo"
        s = rest
    else:
        # 没有列表符号但直接是复选框的情况也接受
        kind = None

    # 复选框
    m_done = CHECKED.match(s)
    m_open = UNCHECKED.match(s)
    if m_done:
        completed = True
        s = s[m_done.end():].lstrip()
        if kind is None:
            kind = "todo"
    elif m_open:
        completed = False
        s = s[m_open.end():].lstrip()
        if kind is None:
            kind = "todo"

    # 时段前缀：@上午 / @ 上午 / 上午: 三种写法都接受
    slot = None
    for cand in SLOTS:
        for pat in (f"@{cand}", f"＠{cand}", f"{cand}：", f"{cand}:"):
            if s.startswith(pat):
                slot = cand
                s = s[len(pat):].lstrip()
                break
        if slot:
            break

    return s, kind, slot


@dataclass
class Entry:
    """一条解析结果。字段设计围绕「不丢信息」和「可人工核对」。"""

    line_no: int          # 在原文中的行号（从 1 起），便于对照
    raw: str              # 原文那一行，逐字保留
    kind: str             # 'todo' | 'note' | 'meta' | 'unknown'
    text: str             # 剥掉标记后的正文
    completed: bool | None = None   # 仅 todo 有意义
    slot: str | None = None         # 时段
    issues: list[str] = field(default_factory=list)  # 需要上层询问/注意的点

    @property
    def norm(self) -> str:
        return normalize_line(self.text)


@dataclass
class ParseResult:
    date: str | None
    entries: list[Entry]

    @property
    def todos(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "todo"]

    @property
    def notes(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "note"]

    @property
    def open_todos(self) -> list[Entry]:
        return [e for e in self.todos if e.completed is not True]

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "counts": {
                "todo": len(self.todos),
                "note": len(self.notes),
                "open_todo": len(self.open_todos),
            },
            "entries": [asdict(e) for e in self.entries],
        }


def parse(text: str) -> ParseResult:
    """
    解析当天页的 plaintext。

    注意：AppleScript 返回的换行可能是 \\n 或 \\r，这里统一处理。
    """
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    entries: list[Entry] = []
    date: str | None = None

    for idx, raw in enumerate(raw_lines, start=1):
        stripped = raw.strip()

        # 空行：不产出条目（但保留在原文里）
        if not stripped:
            continue

        # 元信息行
        if stripped.startswith("#"):
            entries.append(Entry(idx, raw, "meta", stripped.lstrip("# ").strip()))
            continue

        # 第一行且形如日期 → 视为标题（备忘录的 name 就是正文首行）
        if date is None and not entries:
            m = DATE_RE.search(stripped)
            if m and len(stripped) <= 24:
                date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                entries.append(Entry(idx, raw, "meta", date))
                continue

        body, kind, slot = strip_leading_marker(raw)

        if kind is None:
            # 没有标记 → 按约定视为待办（裸行是最高频情况，零符号）
            kind = "todo"

        if not body:
            entries.append(Entry(idx, raw, "unknown", stripped,
                                 issues=["这一行只有标记、没有内容"]))
            continue

        # 标记残留在末尾：常见于用户先写了内容再补标记（"只有标记没内容 - [ ]"），
        # 或者按了回车才想起打勾。剥掉末尾标记，别让它混进正文 ——
        # 否则生成的待办标题会带上 "- [ ]" 这种噪音。
        trailing = re.search(r"\s*[" + re.escape(DASH_CHARS) + r"]?\s*\[\s*[xX✓✔]?\s*\]\s*$", body)
        if trailing and len(body) > trailing.start():
            tail = body[trailing.start():]
            body = body[: trailing.start()].rstrip()
            if re.search(r"\[\s*[xX✓✔]\s*\]", tail):
                completed = True
            else:
                completed = False
            if not body:
                entries.append(Entry(idx, raw, "unknown", stripped,
                                     issues=["这一行只有标记、没有内容"]))
                continue

        completed = None
        if kind == "todo":
            # 需要知道复选框状态：重新扫一遍原文行
            after_marker = raw.strip()
            if after_marker and (after_marker[0] in DASH_CHARS or after_marker[0] in BULLET_CHARS):
                after_marker = after_marker[1:].lstrip()
            if CHECKED.match(after_marker):
                completed = True
            elif UNCHECKED.match(after_marker):
                completed = False
            else:
                # 裸行没有复选框 → 未完成
                completed = False

        entry = Entry(idx, raw, kind, body, completed=completed, slot=slot)

        if slot is None and kind == "todo":
            entry.issues.append("时段未指定，需要询问归属")

        entries.append(entry)

    return ParseResult(date=date, entries=entries)


# ── 去重


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_line(a), normalize_line(b)).ratio()


def find_duplicates(entries: list[Entry], threshold: float = 0.85) -> list[tuple[int, int, float]]:
    """
    找出疑似重复的待办。

    刻意不自动删除：只报告，由上层列给用户确认。
    理由：相似度高不等于重复（"提交结算单A"和"提交结算单B"相似度也很高，
    但它们可能是两件事）。静默去重会丢数据，而丢数据是看不出来的。
    """
    todos = [e for e in entries if e.kind == "todo"]
    out = []
    for i in range(len(todos)):
        for j in range(i + 1, len(todos)):
            r = similar(todos[i].text, todos[j].text)
            if r >= threshold:
                out.append((todos[i].line_no, todos[j].line_no, round(r, 3)))
    return out


# ── 渲染留档


def render_archive(result: ParseResult, meta: dict) -> str:
    """渲染成人可读、可手工修正的留档文件。"""
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("## 原文（逐字保留）")
    lines.append("")
    lines.append("```")
    for e in result.entries:
        lines.append(e.raw.rstrip())
    if not result.entries:
        lines.append("(空)")
    lines.append("```")
    lines.append("")
    lines.append("## 解析结果")
    lines.append("")
    lines.append("| 行 | 类型 | 内容 | 状态 | 时段 | 备注 |")
    lines.append("|---|---|---|---|---|---|")
    for e in result.entries:
        kind_label = {"todo": "待办", "note": "备忘", "meta": "元信息", "unknown": "待定"}.get(e.kind, e.kind)
        if e.kind == "todo":
            state = "已完成" if e.completed else "未完成"
        else:
            state = "—"
        notes = "；".join(e.issues) if e.issues else ""
        lines.append(
            f"| {e.line_no} | {kind_label} | {e.text} | {state} | {e.slot or '—'} | {notes} |"
        )
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 待办 {len(result.todos)} 条（未完成 {len(result.open_todos)} 条）")
    lines.append(f"- 备忘 {len(result.notes)} 条")
    dups = find_duplicates(result.entries)
    if dups:
        lines.append("")
        lines.append("### ⚠️ 疑似重复（需人工确认，程序不会自动删）")
        for a, b, r in dups:
            lines.append(f"- 第 {a} 行 与 第 {b} 行，相似度 {r}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="解析当天页（可读 stdin 或文件）")
    ap.add_argument("path", nargs="?", help="输入文件；省略则读 stdin")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非留档 Markdown")
    ap.add_argument("--archive", metavar="OUT", help="同时写出留档 Markdown 到该路径")
    ap.add_argument("--date", help="覆盖解析出的日期（用于无日期标题的情况）")
    args = ap.parse_args()

    if args.path:
        text = open(args.path, encoding="utf-8").read()
    else:
        text = sys.stdin.read()

    result = parse(text)
    if args.date:
        result.date = args.date

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        meta = {
            "date": result.date or "（未识别）",
            "source": args.path or "stdin",
        }
        print(render_archive(result, meta))

    if args.archive:
        meta = {
            "date": result.date or "（未识别）",
            "source": args.path or "stdin",
        }
        with open(args.archive, "w", encoding="utf-8") as f:
            f.write(render_archive(result, meta))
        print(f"\n[留档已写入 {args.archive}]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
