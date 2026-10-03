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
sync_day：把某天的当天页从备忘录同步到留档。

抽成独立模块的原因：`read_day`（手动读）、`daily_report`（日报前刷新）、
`carry_over`（顺延前确认状态）三处都需要同一步骤。重复实现三遍必然会漂移，
而"三处对同一天的理解不一致"是这类系统最难查的 bug。

留档写两个文件：
  data/days/<日期>.md     人可读、可手工修正
  data/days/<日期>.json   机器读（日报、顺延都读它）
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notes import Notes, NotesError  # noqa: E402
import parse as parser  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DAYS_DIR = ROOT / "data" / "days"


@dataclass
class SyncResult:
    date: str
    note_name: str
    note_id: str
    folder_name: str
    result: parser.ParseResult
    md_path: Path
    json_path: Path


def sync_day(date_str: str, notes: Notes | None = None) -> SyncResult | None:
    """
    读取指定日期的当天页并写留档。返回 None 表示该日期没有当天页。

    抛 NotesError 表示访问失败（权限、文件夹失效等）。
    """
    notes = notes or Notes()
    folder_name, _ = notes.verify_folder()

    note = notes.find_daily_page(date_str)
    if note is None:
        return None

    result = parser.parse(note.plaintext)
    if result.date is None:
        result.date = date_str

    # 保留上一次解析出的 completed。
    #
    # ⚠️ 为什么必须做：completed 的权威是**提醒事项**，而重新解析只能看到
    # 备忘录里的 `[ ]`/`[x]`。不保留的话，每次 --refresh/日报同步都会把
    # 权威状态冲成"未完成" —— 于是顺延又把已完成的事带一遍（实测踩到）。
    #
    # 键用**内容指纹**（不用行号）：行号会随编辑位移，而内容才是身份。
    _prev = load_archive(date_str)
    if _prev is not None:
        _prev_done = {
            parser.content_fingerprint(e.text): e.completed
            for e in _prev.entries if e.kind == "todo" and e.completed is not None
        }
        for e in result.entries:
            if e.kind != "todo":
                continue
            fp = parser.content_fingerprint(e.text)
            if fp in _prev_done and e.completed is not True:
                e.completed = _prev_done[fp]

    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = DAYS_DIR / f"{date_str}.md"
    json_path = DAYS_DIR / f"{date_str}.json"

    meta = {
        "date": date_str,
        "source": f"备忘录 [{folder_name}] / {note.name}",
        "note_id": note.id,
        "read_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    md_path.write_text(parser.render_archive(result, meta), encoding="utf-8")

    payload = result.to_dict()
    payload["_meta"] = meta
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return SyncResult(
        date=date_str,
        note_name=note.name,
        note_id=note.id,
        folder_name=folder_name,
        result=result,
        md_path=md_path,
        json_path=json_path,
    )


def load_archive(date_str: str) -> parser.ParseResult | None:
    """
    从留档 JSON 读取（不访问备忘录）。

    与 sync_day 的分工：需要**当前真实状态**时用 sync_day；
    只是想看"上次读到的快照"时用 load_archive（更快，且不受备忘录变动影响）。
    """
    p = DAYS_DIR / f"{date_str}.json"
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    entries = [
        parser.Entry(
            line_no=e["line_no"],
            raw=e["raw"],
            kind=e["kind"],
            text=e["text"],
            completed=e.get("completed"),
            issues=list(e.get("issues") or []),
        )
        for e in data["entries"]
    ]
    return parser.ParseResult(date=data.get("date"), entries=entries)


def save_archive(date_str: str, result: parser.ParseResult) -> Path:
    """
    把（可能被回填过的）解析结果写回留档 JSON。

    为什么需要它：完成状态的权威在提醒事项，而**顺延在次日早上读的是留档**。
    如果 21:30 解析出的状态只留在内存里，顺延看到的就还是备忘录里的旧标记 ——
    于是你昨晚打的钩会失效，做完的事被顺延到第二天。

    counts 由 ParseResult 现算，所以会跟着更新后的状态走。
    """
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DAYS_DIR / f"{date_str}.json"
    payload = result.to_dict()
    meta = archive_meta(date_str)
    if meta:
        payload["_meta"] = meta
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return json_path


def archive_meta(date_str: str) -> dict:
    p = DAYS_DIR / f"{date_str}.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8")).get("_meta", {})
