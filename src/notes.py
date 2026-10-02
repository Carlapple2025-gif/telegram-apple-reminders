#!/usr/bin/env python3
"""
备忘录读写。

⚠️ 两个必须遵守的约束（都来自实测，违反会产生静默错误）：

1. **必须限定文件夹，不能用全局 `every note`。**
   实测：删除的备忘录会进「Recently Deleted」文件夹并保留 30 天，
   而 `count of notes` 与 `every note` **仍然会返回它**，且它排在第一位。
   如果按"第一条就是最新"来读当天页，会读到一条已删除的僵尸笔记，
   然后把它顺延到明天 —— 错得看不出来。
   所以一律用 `notes of folder id "..."`。

2. **写入之后必须读回验证。**
   实测：备忘录存在「命令不报错但什么都没做」的静默失败。
   只看有没有抛错会把失败当成成功。

定位方式：用**文件夹 id**（不是名字）。改名不影响，同名也不会选错。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


class NotesError(RuntimeError):
    pass


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise NotesError(
            f"缺少配置文件 {CONFIG_PATH}\n请先运行：bash deploy/init.sh"
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("folder_id"):
        raise NotesError(
            f"{CONFIG_PATH} 里没有 folder_id\n请先运行：bash deploy/init.sh"
        )
    return cfg


# ── AppleScript 执行


def _as_literal(s: str) -> str:
    """转成 AppleScript 字符串字面量。反斜杠必须最先转义。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_applescript(source: str) -> str:
    """
    通过 osascript 执行 AppleScript。

    为什么走子进程而不是 NSAppleScript：
      TCC 授权绑定调用进程身份。osascript 用的是**终端**的授权，稳定不变；
      而本仓库如果做成 app bundle，ad-hoc 签名每次重新编译哈希都会变，
      授权随之失效，需要反复重新授权。
    """
    proc = subprocess.run(
        ["osascript", "-e", source],
        capture_output=True,
        text=True,
    )
    out = proc.stdout.strip()
    err = proc.stderr.strip()

    if err:
        # 把常见错误码翻译成人话，避免上层看到天书
        if "-10004" in err or "privilege violation" in err:
            raise NotesError(
                "备忘录拒绝访问（-10004 越权）。\n"
                "请到 系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「备忘录」"
            )
        if "-1743" in err:
            raise NotesError(
                "系统不允许向「备忘录」发送 Apple 事件（-1743）。\n"
                "同上，需要在「自动化」里授权。"
            )
        raise NotesError(f"AppleScript 失败：{err}")

    return out


@dataclass
class Note:
    id: str
    name: str
    plaintext: str


class Notes:
    """
    限定在某个文件夹内的备忘录访问。

    所有查询都带 `of folder id "..."`，从结构上杜绝读到「最近删除」。
    """

    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        self.folder_id = self.config["folder_id"]
        self.folder_name = self.config.get("folder_name", "?")

    # ── 校验

    def verify_folder(self) -> tuple[str, int]:
        """确认配置的文件夹 id 仍然有效。返回 (当前名称, 条数)。"""
        out = run_applescript(
            f'tell application "Notes"\n'
            f'  set hits to (every folder whose id is {_as_literal(self.folder_id)})\n'
            f'  if (count of hits) is 0 then return "MISSING"\n'
            f'  set f to item 1 of hits\n'
            f'  return (name of f) & "|" & (count of notes of f)\n'
            f'end tell'
        )
        if out == "MISSING":
            raise NotesError(
                f"配置里的日志文件夹已不存在（id={self.folder_id}）。\n"
                f"可能被删除了。请重新运行：bash deploy/init.sh"
            )
        name, _, count = out.partition("|")
        return name.strip(), int(count.strip() or 0)

    # ── 读

    def list_notes(self) -> list[tuple[str, str]]:
        """列出该文件夹内所有条目的 (id, name)。"""
        # 用「id 一行、name 一行」成对输出，避免在 AppleScript 字面量里嵌控制字符
        # （控制字符在 AppleScript 源码中无法可靠转义，这是踩过的坑）。
        out = run_applescript(
            f'tell application "Notes"\n'
            f'  set out to ""\n'
            f'  repeat with n in notes of folder id {_as_literal(self.folder_id)}\n'
            f'    set out to out & (id of n) & linefeed & (name of n) & linefeed\n'
            f'  end repeat\n'
            f'  return out\n'
            f'end tell'
        )
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        rows = []
        for i in range(0, len(lines) - 1, 2):
            rows.append((lines[i], lines[i + 1]))
        return rows

    def names(self) -> list[str]:
        """该文件夹内所有条目的标题。"""
        return [nm for _, nm in self.list_notes()]

    def note_id_for(self, name: str) -> str | None:
        """
        按标题取 id。标题需**精确匹配**（备忘录的 name 就是正文首行）。

        补充一点实测结论：不能用 `every note whose name is "..."` 来查
        （那样查到 0 条），但**在循环里逐个比较 name 是可行的** ——
        这个区别很反直觉，所以这里刻意写成循环。
        """
        out = run_applescript(
            'tell application "Notes"\n'
            f'  repeat with n in notes of folder id {_as_literal(self.folder_id)}\n'
            f'    if (name of n) is {_as_literal(name)} then return id of n\n'
            '  end repeat\n'
            '  return "NOTFOUND"\n'
            'end tell'
        )
        return None if out == "NOTFOUND" else out

    def plaintext_of(self, note_id: str) -> str | None:
        """
        按 id 取正文纯文本。

        id 是唯一可靠的定位方式 —— 实测 `whose id is` 能命中，
        而按 name 查会返回 0 条。
        """
        out = run_applescript(
            'tell application "Notes"\n'
            f'  set hits to (every note of folder id {_as_literal(self.folder_id)} '
            f'whose id is {_as_literal(note_id)})\n'
            '  if (count of hits) is 0 then return "NOTFOUND"\n'
            '  return plaintext of item 1 of hits\n'
            'end tell'
        )
        return None if out == "NOTFOUND" else out

    def get_by_name(self, name: str) -> Note | None:
        """按名称取条目。名称需精确匹配（备忘录的 name 就是正文首行）。"""
        nid = self.note_id_for(name)
        if nid is None:
            return None
        text = self.plaintext_of(nid)
        if text is None:
            return None
        return Note(id=nid, name=name, plaintext=text)

    def get(self, name: str) -> Note | None:
        """按标题取条目（get_by_name 的简名）。

        之所以保留两个名字：`get` 更短、在调用处更自然；但语义上与
        get_by_name 完全相同，所以实现里直接委托，避免逻辑分叉。
        """
        return self.get_by_name(name)

    def get_by_id(self, note_id: str) -> Note | None:
        text = self.plaintext_of(note_id)
        if text is None:
            return None
        nm = self.name_of(note_id) or ""
        return Note(id=note_id, name=nm, plaintext=text)

    def name_of(self, note_id: str) -> str | None:
        out = run_applescript(
            'tell application "Notes"\n'
            f'  set hits to (every note of folder id {_as_literal(self.folder_id)} '
            f'whose id is {_as_literal(note_id)})\n'
            '  if (count of hits) is 0 then return "NOTFOUND"\n'
            '  return name of item 1 of hits\n'
            'end tell'
        )
        return None if out == "NOTFOUND" else out

    def find_daily_page(self, date_str: str) -> Note | None:
        """
        找某天的当天页。标题以日期开头即算命中（容忍你在日期后加备注）。

        **只在该文件夹内查找** —— 这是关键，见模块头部说明。
        实现上分两步（先按标题拿 id，再按 id 拿正文），而不是拼一个大字符串：
        AppleScript 字面量里嵌控制字符不可靠，分步走更稳。
        """
        for nm in self.names():
            if nm.startswith(date_str):
                nid = self.note_id_for(nm)
                if nid is None:
                    continue
                text = self.plaintext_of(nid)
                if text is None:
                    continue
                return Note(id=nid, name=nm, plaintext=text)
        return None

    # ── 写（全部带读回验证）

    def create(self, body_lines: list[str]) -> Note:
        """
        新建条目。

        正文用 <div> 包每一行 —— 实测必需：直接把多行字符串交给备忘录会被
        折叠成一行（"标题 - [ ] 甲 - [x] 乙 * 丙"），既难读也无法按行解析。
        """
        html = "".join(f"<div>{_html_escape(line)}</div>" for line in body_lines)
        title = body_lines[0] if body_lines else ""

        out = run_applescript(
            f'tell application "Notes"\n'
            f'  make new note at folder id {_as_literal(self.folder_id)} '
            f'with properties {{body:{_as_literal(html)}}}\n'
            f'  return "made"\n'
            f'end tell'
        )
        if out != "made":
            raise NotesError(f"创建失败，返回：{out}")

        # 读回验证 —— 备忘录存在"不报错但没建成"，不能只看返回值
        note = self.get_by_name(title)
        if note is None:
            raise NotesError(
                f"创建命令没报错，但读回找不到标题为「{title}」的条目 —— "
                f"写入未生效（备忘录的静默失败）"
            )
        return note

    def append_lines(self, note_id: str, lines: list[str]) -> Note:
        """
        往已有条目末尾追加若干行（n+1 顺延的核心动作）。

        追加后读回验证标记是否出现 —— 实测 set body 可能不报错但无效。
        """
        if not lines:
            return self.get_by_id(note_id) or Note(id=note_id, name="", plaintext="")

        html = "".join(f"<div>{_html_escape(line)}</div>" for line in lines)
        run_applescript(
            f'tell application "Notes"\n'
            f'  set hits to (every note of folder id {_as_literal(self.folder_id)} '
            f'whose id is {_as_literal(note_id)})\n'
            f'  if (count of hits) is 0 then return "NOTFOUND"\n'
            f'  set n to item 1 of hits\n'
            f'  set body of n to (body of n) & {_as_literal(html)}\n'
            f'  return "ok"\n'
            f'end tell'
        )

        note = self.get_by_id(note_id)
        if note is None:
            raise NotesError(f"追加后读回找不到条目 {note_id}")
        # 用第一行的纯文本做存在性检查（HTML 会被规范化，直接比字符串不可靠）
        probe = re.sub(r"<[^>]+>", "", lines[0]).strip()
        if probe and probe not in note.plaintext:
            raise NotesError(
                f"追加命令没报错，但读回看不到追加内容（找不到「{probe}」）—— "
                f"写入未生效"
            )
        return note


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── CLI

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="备忘录访问（限定日志文件夹）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify", help="确认文件夹 id 有效并列出条数")
    sub.add_parser("list", help="列出日志文件夹内的所有条目")

    p_get = sub.add_parser("get", help="按日期读取当天页并打印 plaintext")
    p_get.add_argument("date", help="日期，如 2026-10-02")

    p_name = sub.add_parser("get-name", help="按标题读取并打印 plaintext")
    p_name.add_argument("name")

    args = ap.parse_args()
    notes = Notes()

    try:
        if args.cmd == "verify":
            name, count = notes.verify_folder()
            print(f"✅ 文件夹有效：当前名称「{name}」，{count} 条")
            print(f"   folder_id = {notes.folder_id}")
            return 0

        if args.cmd == "list":
            rows = notes.list_notes()
            print(f"日志文件夹「{notes.folder_name}」共 {len(rows)} 条：")
            for nid, nm in rows:
                print(f"  · {nm}")
                print(f"      {nid}")
            return 0

        if args.cmd == "get":
            note = notes.find_daily_page(args.date)
            if note is None:
                print(f"该文件夹内没有以「{args.date}」开头的条目", file=sys.stderr)
                return 1
            sys.stdout.write(note.plaintext)
            return 0

        if args.cmd == "get-name":
            note = notes.get_by_name(args.name)
            if note is None:
                print(f"该文件夹内没有标题为「{args.name}」的条目", file=sys.stderr)
                return 1
            sys.stdout.write(note.plaintext)
            return 0

    except NotesError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
