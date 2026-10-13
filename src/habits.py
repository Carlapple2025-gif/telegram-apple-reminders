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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class HabitWeek:
    """
    一个习惯**这一周**的状态。日报只读它渲染 —— 统计不散进渲染里。

    `in_list` 回答的是"标准表里的这个名字，习惯列表里还有吗"：
    改名之后标准会**永远**显示 0/N，那是**假的差异** —— 比不报更糟
    （用户会以为系统坏了，或者以为自己在偷懒）。
    """
    name: str
    target: int
    done: int
    streak: int
    in_list: bool = True

    @property
    def gap(self) -> int:
        return max(0, self.target - self.done)

    @property
    def met(self) -> bool:
        return self.done >= self.target


def standards() -> dict[str, int]:
    """
    `config.json` 的 `habits_standards`：习惯名 → **每周目标次数**。

    为什么标准必须由人声明（不能从 Apple 推）：AppleScript 读不出重复规则，
    而且没打钩的重复条目**不会前滚**（APPLE-FACTS §2.5）—— 于是"这周该做几次"
    **推不出来**。与 `calendar_name` 同一个模式：机器执行，改动是人工动作。

    **空 = 没声明 → 日报那一段不出现**（不猜，也不拿默认值糊上去）。
    """
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 配置坏了不该让日报整份失败
        return {}
    raw = cfg.get("habits_standards")
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            try:
                n = int(val)
            except (TypeError, ValueError):
                continue
            if str(key).strip() and n > 0:
                out[str(key).strip()] = n
    return out


def week_start(day: dt.date) -> dt.date:
    """本周一（`date.weekday()` 周一 = 0）。"""
    return day - dt.timedelta(days=day.weekday())


def _iso(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _streak(days: set[dt.date], today: dt.date) -> int:
    """连续有完成的天数。**今天没做不算断**（从昨天往前数）—— 同周报口径。"""
    if not days:
        return 0
    cur = today if today in days else today - dt.timedelta(days=1)
    n = 0
    while cur in days:
        n += 1
        cur -= dt.timedelta(days=1)
    return n


def week_status(now: dt.datetime, *, list_name: str, targets: dict[str, int],
                window_days: int = WINDOW_DAYS) -> list[HabitWeek]:
    """
    本周每个习惯：完成次数 / 目标 / 连续天数。

    两处来源，按 **habit_id 精确判重**（两边都带 id，所以判重不是猜的）：

      · journal 留档 —— 周一 → 今天（不受窗口限制，跨重启）
      · Apple 最近窗口 —— 补**扫描点（21:00）之后**打的钩

    为什么必须并上第二处：扫描在 21:00，日报在 21:30。只读留档会把
    21:05 刚打完的钩报成"还差一次" —— 那是**假的差异**，比不报更糟 ✗。

    `open_reminders()` 那一次读只用来判"标准里写了、列表里却没有"；
    它失败**不致命**（降级为"不判 missing"），所以单独吞掉。
    """
    import journal
    import reminders

    start = week_start(now.date())
    today = now.date()
    span = (today - start).days + 2      # 多两天：覆盖"晚一天才扫到"的

    names_of: dict[str, str] = {}
    week_ids: set[str] = set()
    days_with: dict[str, set[dt.date]] = {}

    def _add(hid: str, name: str, when: dt.datetime | None) -> None:
        if not hid or not name:
            return
        names_of.setdefault(hid, name)
        if when is None:
            return
        # **连续天数不限本周**（连续 12 天要跨周才数得出来）。
        days_with.setdefault(name, set()).add(when.date())
        # ⚠️ 但**计数必须落在本周**。两个来源都是"最近 N 天"，
        # 那不等于"本周" —— 不加这一层，周一/周二的日报会把上周日的完成
        # 算进本周（2026-10-14 审计实测：周一 + 一条周日记录 → 1/5，应为 0/5），
        # 而 habits_window_days 最大可到 30 天，会放得更大。
        if start <= when.date() <= today:
            week_ids.add(hid)

    for rec in journal.habit_records(days=span):
        _add(str(rec.get("habit_id") or ""), str(rec.get("name") or ""),
             _iso(str(rec.get("done_at") or rec.get("at") or "")))

    rem = reminders.Reminders({"reminders_list": list_name})
    for r in rem.completed_since(window_days):     # 读失败 → 交给上层报 SourceError
        _add(r.id, r.name, r.completed_at)

    try:
        present = {r.name for r in rem.open_reminders()}
    except Exception:  # noqa: BLE001 —— 附加检查，读不到就不判 missing
        present = set()

    out: list[HabitWeek] = []
    for name, target in targets.items():
        done = sum(1 for hid, nm in names_of.items()
                   if nm == name and hid in week_ids)
        out.append(HabitWeek(name=name, target=target, done=done,
                             streak=_streak(days_with.get(name, set()), now.date()),
                             in_list=(name in present) or done > 0))
    # 未达成的、差得多的排前面；达成的沉底（日报要让人先看见欠账）
    out.sort(key=lambda h: (h.met, -h.gap, h.name))
    return out


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
