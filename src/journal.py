#!/usr/bin/env python3
"""
用户日志：传感器读数（追加写入，永不修改）。

## 这个模块的定位（关键，别搞错）

journal **不是状态源**，是**传感器读数** —— 类似并联在电路中的电压表：

    ┌──────────────────┐
    │  Apple 应用       │ ← 状态源（唯一真相）
    └────────┬─────────┘
             │ 只读快照
             ▼
        journal（读数记录）

**它没有"当前状态"这个概念。** "某条备忘还在不在"永远从 Apple 应用现读，
journal 只回答"我提交过什么"和"我观察到什么"。

## 为什么不做镜像副本

曾经的方案是让 journal 保存一份待办的**镜像**，用它判断"该提醒什么"。
那有两个必然缺陷：

  ① 你在 Apple 应用里删掉一条 → 镜像不知道 → 若用它重建，会把删掉的造回来
  ② 镜像里那条还在 → 日报提醒你一件早已处理完的事

根因：镜像是**第二份状态**，两份一旦不同就得猜谁对 —— 回到"猜身份"老坑。
（用户的比喻很准：那是"左脚踩右脚"的死循环。）

## 两条铁律

| 机制 | 记什么 | 绝不用于 |
|---|---|---|
| 动作日志 | "我提交过什么" | 判断某条是否还存在 |
| 观察日志 | "我读到过什么" | 决定下次怎么写 |

读取路径只产生**观察记录**，不反馈进写入路径 —— 所以不构成闭环。

## 存储

    data/journal/YYYY-MM-DD.jsonl   一天一个文件，一行一条 JSON

**追加式**：只 append，从不修改已有行。这样即使程序出错，
历史也不会被破坏（可以审计"当时到底发生了什么"）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

# 项目根（本文件在 src/ 下）
ROOT = Path(__file__).resolve().parent.parent
JOURNAL_DIR = ROOT / "data" / "journal"

# 事件类型。用常量避免拼错 —— 拼错的事件类型会静默地永远匹配不上。
EV_INPUT = "input"            # 你的原始输入（原样保存，便于复盘）
EV_TODO_ADDED = "todo_added"
EV_EVENT_ADDED = "event_added"
EV_MEMO_ADDED = "memo_added"
EV_MEMO_CLEARED = "memo_cleared"
EV_OBSERVED = "observed"      # 一次快照观察的结果
EV_ERROR = "error"            # 处理失败（供排查）

# 会用到的日期键（某些事件跨天出现，需要单独记）
DATE_KEY = "for_date"


def _today() -> str:
    return dt.date.today().isoformat()


def _path_for(date_str: str) -> Path:
    return JOURNAL_DIR / f"{date_str}.jsonl"


def now_iso() -> str:
    """本地时间的 ISO 字符串（带时区偏移，便于日后跨时区核对）。"""
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def append(event: str, **fields) -> dict:
    """
    追加一条记录。返回写入的那条（便于调用方回显/测试）。

    ⚠️ 这个函数**只追加**，不会读取或修改任何已有内容 ——
    这是"传感器"定位的直接体现。哪怕传入的数据是错的，
    也只是多一条错记录，不会破坏历史。
    """
    rec = {"at": now_iso(), "event": event}
    rec.update(fields)

    date_str = fields.get(DATE_KEY) or _today()
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = _path_for(date_str)

    # 一行一条 JSON（ensure_ascii=False 保留中文可读性；
    # separators 去掉多余空格，文件更小也更好肉眼扫）
    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())      # 立刻落盘：日志丢了就找不回来了

    return rec


# ── 便捷包装（让调用处读起来像业务语言，而不是一堆字符串）

def log_input(text: str, source: str = "telegram", msg_id: int | None = None) -> dict:
    """记下你的原始输入。**原样保存**，不做任何加工。"""
    return append(EV_INPUT, text=text, source=source, msg_id=msg_id)


def log_todo(text: str, reminder_id: str = "", ok: bool = True,
             detail: str = "") -> dict:
    return append(EV_TODO_ADDED, text=text, reminder_id=reminder_id,
                  ok=ok, detail=detail)


def log_event(summary: str, start: str = "", end: str = "",
              location: str = "", calendar: str = "", ok: bool = True,
              detail: str = "") -> dict:
    return append(EV_EVENT_ADDED, summary=summary, start=start, end=end,
                  location=location, calendar=calendar, ok=ok, detail=detail)


def log_memo(text: str, memo_id: str, ok: bool = True, detail: str = "") -> dict:
    """
    记下"我提交过一条备忘"。

    `memo_id` 是 agent 生成并写进备忘录的标识 —— 它是**观察阶段的唯一凭据**：
    下次只读快照时，靠它判断"我写的那条还在不在"。
    """
    return append(EV_MEMO_ADDED, memo_id=memo_id, text=text, ok=ok, detail=detail)


def log_memo_cleared(memo_id: str, text: str = "") -> dict:
    """记下"快照里发现这条不在了"（即你已删除）。此后永不再提醒。"""
    return append(EV_MEMO_CLEARED, memo_id=memo_id, text=text)


def log_observed(kind: str, present: int = 0, cleared: list | None = None,
                 note: str = "") -> dict:
    """记下一次观察（快照读到了什么、与台账的差集是什么）。"""
    return append(EV_OBSERVED, kind=kind, present=present,
                  cleared=cleared or [], note=note)


def log_error(where: str, detail: str) -> dict:
    return append(EV_ERROR, where=where, detail=detail)


# ── 读取（供复盘与"我提交过什么"）

def read_day(date_str: str) -> list[dict]:
    """读某天的全部记录。文件不存在时返回空列表（不是错误）。"""
    path = _path_for(date_str)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # 单行坏了不该让整个日志读不出来
            continue
    return out


def read_range(days: int, end: str | None = None) -> list[dict]:
    """读最近 `days` 天的记录（含当天），按时间先后。"""
    end_date = dt.date.fromisoformat(end) if end else dt.date.today()
    out: list[dict] = []
    for i in range(days - 1, -1, -1):
        d = (end_date - dt.timedelta(days=i)).isoformat()
        out.extend(read_day(d))
    return out


def submitted_memos(days: int = 365, end: str | None = None) -> dict[str, dict]:
    """
    从日志里重建「我提交过哪些备忘」的台账。

    返回 {memo_id: {text, at, for_date}} —— **只含尚未观察到被清除的**。

    注意这**不是**"当前状态"，而是"我提交过、还没观察到你删除的"。
    "它现在还在不在"必须由只读快照回答（见 memo.py）。
    这个区分是整个架构的关键，别把两者混起来用。
    """
    added: dict[str, dict] = {}
    cleared: set[str] = set()

    for rec in read_range(days, end):
        ev = rec.get("event")
        mid = rec.get("memo_id")
        if not mid:
            continue
        if ev == EV_MEMO_ADDED:
            added[mid] = {"text": rec.get("text", ""), "at": rec.get("at", ""),
                          "for_date": rec.get("for_date") or rec.get("at", "")[:10]}
        elif ev == EV_MEMO_CLEARED:
            cleared.add(mid)

    return {k: v for k, v in added.items() if k not in cleared}


def main() -> int:
    """命令行自检：追加一条测试记录并读回。"""
    import argparse

    ap = argparse.ArgumentParser(description="用户日志（传感器读数）")
    ap.add_argument("--show", metavar="DATE", nargs="?", const="today",
                    help="打印某天的记录（默认今天）")
    ap.add_argument("--test", action="store_true", help="追加一条测试记录")
    args = ap.parse_args()

    if args.test:
        rec = log_input("这是一条测试输入（可忽略）", source="selftest")
        print(f"✅ 已追加：{json.dumps(rec, ensure_ascii=False)}")
        print(f"   文件：{_path_for(_today()).relative_to(ROOT)}")
        return 0

    date_str = _today() if args.show in (None, "today") else args.show
    recs = read_day(date_str)
    print(f"{date_str}：{len(recs)} 条记录")
    print(f"文件：{_path_for(date_str).relative_to(ROOT)}")
    for r in recs:
        ev = r.get("event", "?")
        rest = {k: v for k, v in r.items() if k not in ("event", "at")}
        print(f"  {r.get('at','')[:19]}  {ev:<14} {json.dumps(rest, ensure_ascii=False)[:80]}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
