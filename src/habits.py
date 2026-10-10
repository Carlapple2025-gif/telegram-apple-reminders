#!/usr/bin/env python3
"""
习惯留档：在守护进程里"每晚把提醒事项里的完成记录抄进 journal"。

## 它补的是哪个洞

"一周 5/7"这种统计需要**每一次发生的完成记录**，而它只有两个来源，
都不能只靠 Apple：

| 只靠 Apple 的问题 | 依据 |
|---|---|
| **历史会被清空** —— 你在提醒事项点一下"清除已完成"，过去就没了 | —— |
| **逐条明细很贵** —— 实测 ~1 s/条，攒到 100 条就是 100 秒 | APPLE-FACTS §2.5 |

所以走"抄一遍"：**每晚只读一个"最近 N 天"的窗口**（原生 ¶whose¶ 服务端筛，
实测 595 ms），把**新出现**的完成记录写进 journal；
此后所有周 / 月统计都从 journal 算 ——
这正是"留档独立于 Apple 应用"那条原则的第三个用途。

## 三条设计约束（与 watchdog.py 同源）

1. **不加状态文件**。"今天扫过没有"从 journal 的 ¶habit_scanned¶ 读数里看
   （同 ¶delivered_on()¶ 看 ¶digest_pushed¶）。
2. **绝不抛异常**。它跑在守护的收件循环里，一次读取失败绝不能把收件搞停；
   但也不静默 —— 失败会留下一行日志。
3. **每天最多一次 + 按 ¶habit_id¶ 判重**。窗口重叠、机器补醒、手动重跑都安全。

## 为什么由守护写（而不是日报）

部署那张表写着：**daemon = 唯一写入者，report = 只读**。
让日报写 journal 会破坏那条不变量 —— 而它正是"留档可信"的前提。

## 它不做什么

· **不算**"本周该做几次" —— 那个读不出来，标准由你在 config 里声明
  （见 docs/CADENCE.md §五）；
· **不区分**档位（正常 / 最低写在提醒的备注里，系统只数次数）；
· **不删**任何东西（journal 只追加）。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"

# 过了这个点才扫。
#
# 为什么不是 23:30（看门狗那个点）：日报 **21:30** 生成，它要读"今天"的完成记录，
# 而习惯一般落在 20:00–20:30 —— 21:00 扫一遍刚好赶在日报之前。
# 晚睡的补做会落进**第二天**那次扫描（窗口 3 天，不会丢，只是晚一天进报表）。
SCAN_FROM = dt.time(21, 0)

# 读最近几天的窗口。为什么不是 1 天：
#   · 机器可能整晚没醒（醒来时"今天"已经不是那天了）；
#   · 守护可能被重启、被升级。
# 重叠读取 + 按 id 判重 = **不漏也不重**（代价是每天多读几条，可忽略）。
WINDOW_DAYS = 3

MAX_WINDOW_DAYS = 30


def _log(msg: str) -> None:
    print(f"  [habits] {msg}", file=sys.stderr, flush=True)


def config() -> tuple[str, int]:
    """
    读 config.json：习惯列表名 + 窗口天数。

    **列表名为空 = 未启用**（不扫、不报错）—— 同 ¶reminders_list¶ 的处理：
    没配置不是故障，只是这条功能没开。
    """
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 配置坏了不该让收件停
        return "", WINDOW_DAYS
    name = str(cfg.get("habits_list") or "").strip()
    try:
        days = int(cfg.get("habits_window_days") or WINDOW_DAYS)
    except (TypeError, ValueError):
        days = WINDOW_DAYS
    return name, max(1, min(days, MAX_WINDOW_DAYS))


def should_scan(now: dt.datetime, *, list_name: str,
                scanned_today: bool) -> tuple[bool, str]:
    """
    要不要扫（**纯函数**：不读文件、不碰 Apple，方便自检直接断言）。

    返回 (扫不扫, 原因)。原因会进日志 —— 一句"为什么没扫"比沉默有用得多。
    """
    if not list_name:
        return False, "未配置 habits_list"
    if scanned_today:
        return False, "今天已经扫过"
    if now.time() < SCAN_FROM:
        return False, f"还没到 {SCAN_FROM:%H:%M}"
    return True, ""


def scan(*, list_name: str, days: int) -> dict:
    """
    读窗口 → 判重 → 写 journal。返回 ¶{"found", "added", "skipped"}¶。

    判重靠 ¶journal.habit_ids()¶（最近若干天的 ¶habit_done¶ 读数），
    **不是**靠一个"见过的 id"状态文件 —— 读数是读数，不是状态源。
    """
    import journal
    import reminders

    rem = reminders.Reminders({"reminders_list": list_name})
    found = rem.completed_since(days)
    seen = journal.habit_ids(days + 2)   # 多往前看两天：覆盖"晚一天才扫到"的

    added = skipped = 0
    for r in found:
        if not r.id:
            continue
        if r.id in seen:
            skipped += 1
            continue
        done_at = r.completed_at.isoformat() if r.completed_at else ""
        journal.log_habit_done(r.id, r.name, done_at=done_at,
                               list_name=list_name)
        seen.add(r.id)
        added += 1

    journal.log_habit_scanned(list_name=list_name, found=len(found),
                              added=added, skipped=skipped)
    return {"found": len(found), "added": added, "skipped": skipped}


def tick(now: dt.datetime | None = None) -> None:
    """
    守护循环里调它：到点、今天没扫过、且配了列表，就扫一次。

    **吞掉一切异常**（约束 2）—— 但会留下一行日志（不静默失败）。
    """
    try:
        import journal

        list_name, days = config()
        now = now or dt.datetime.now()
        scanned = journal.habit_scanned_on(now.date().isoformat())
        ok, why = should_scan(now, list_name=list_name, scanned_today=scanned)
        if not ok:
            return
        res = scan(list_name=list_name, days=days)
        _log(f"已留档：读到 {res['found']} 条，新增 {res['added']}，"
             f"判重跳过 {res['skipped']}（{list_name}，近 {days} 天）")
    except Exception as e:  # noqa: BLE001 —— 故意的：附加组件不许带停收件
        _log(f"扫描失败（不影响收件）：{type(e).__name__}: {e}")


def main() -> int:
    """手动跑一次（排查用）：python3 src/habits.py [--force]"""
    args = sys.argv[1:]
    name, days = config()
    if not name:
        print("未配置 habits_list（config.json）—— 这条功能没开")
        return 0
    if "--force" in args:
        res = scan(list_name=name, days=days)
        print(f"强制扫描：{res}")
        return 0
    tick()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
