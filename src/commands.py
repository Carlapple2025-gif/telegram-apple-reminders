#!/usr/bin/env python3
"""
命令层：**只读**查询。目前只有一条 —— 看现在的待办与日程。

    你发： /list
    别名： /ls  /today  /now  /todo  /列表  /待办  /日程

## 判据是 `/`，不是词

行首字符在这个系统里一直是**声明**：`#` 备忘、`@` 日程、裸输入待办。
命令是第四种声明：**`/` = "这是一次查询，不是要记的内容"**。

为什么必须有 `/`：`list` 或 `列表` 完全可能是**待办的正文**
（"list" 可以是你要买的东西），而 `/` 开头的正文几乎不存在。
所以 `/` 不是装饰，它是这条命令唯一的判据 ——
与符号方案同源：**类型由你声明，不由代码猜。**

## 为什么不写进 routes.py / intake.py

  · `routes.py` 只决定"写到哪个 App"，而命令**哪个都不写**；
  · `intake.py` 的契约是"只增"（架构定死），不该承担读操作；
  · 所以单独成模块，由守护在**通道层**分流：判据是**字面**的
    （以 `/` 开头），不是业务判断 —— 与"这条不是文字"同级。

这与 2026-10-04 否决 `/help` 不冲突：那次拒的是"再引入一套与符号并行的
词汇表（`/memo` `/event`…）"；这里只有**一条**命令，
而且它是整个系统里唯一的只读操作。

## 只读，且失败要如实说

读取**复用** `report.py` 里那两条读函数，不另写一份 ——
"同一件事两处实现"是本项目反复踩过的坑。
某处读不到就如实写 `⚠️ …读不到`：**绝不把"读不到"说成"没有"**
（与日报同一条原则，日报为此专门有断言）。

## 留痕去哪

命令**不写 journal**：journal 是你的输入留痕（传感器读数，不可再生），
而命令是读操作、不产生任何要复盘的内容 ——
它的留痕属于 `logs/`（程序日志，可随时清）。
这条线是 journal.py 自己画的（见它给 `digest_pushed` 写的说明）。
"""

from __future__ import annotations

import datetime as dt

import whens

# 命令名（小写）→ 规范名。目前所有别名都指向同一条命令：
# 保持"一条命令"是有意的 —— 每多一条命令，就多一份要在文档、
# 启动通知、自检三处同步的词汇（`/help` 就是因此被否决的）。
ALIASES: dict[str, str] = {
    "list": "list", "ls": "list", "today": "list", "now": "list",
    "todo": "list", "列表": "list", "待办": "list", "日程": "list",
}

# `match()` 的第三种返回值：以 `/` 开头，但不认识。
UNKNOWN = "?"

# 一屏最多列多少条。超了就说"还有 N 条" —— 不刷屏，也不隐瞒总数。
MAX_ITEMS = 15

USAGE = "可用命令：/list（= /ls /today /列表）—— 看现在的待办与日程"

# 要在 Telegram 的 `/` 菜单里出现的命令（守护启动时推上去，见 telegram.set_my_commands）。
#
# 只列**规范名**，不列别名：菜单是"发现性"入口，把 8 个别名都塞进去
# 只会让人以为有 8 条命令（它们全指向同一条）。
#
# Telegram 的硬约束：名字只能 `[a-z0-9_]`、1–32 字符；描述 3–256 字符。
# 自检里有一条断言按这两条规则校验 —— 不合规的话 API 会直接报错，
# 而那种错误在启动日志里只表现为"菜单设置失败"，很难查。
MENU: list[tuple[str, str]] = [
    ("list", "看现在的待办与日程"),
]


def match(text: str) -> str | None:
    """
    这串输入是不是一条命令？

    返回：命令名 / `UNKNOWN`（以 `/` 开头但不认识）/ `None`（不是命令，照常收件）。

    **纯字面判断**：不看语义、不看上下文、不读时钟 —— 所以它不需要任何状态。
    调用方（守护）据此分流，判断的"重量"与"这条消息不是文字"同级。
    """
    s = (text or "").strip()
    if not s.startswith("/"):
        return None
    rest = s[1:].strip()
    if not rest:
        return UNKNOWN
    word = rest.split()[0].lower()
    word = word.split("@", 1)[0]        # 群里发过来会是 /list@botname
    return ALIASES.get(word, UNKNOWN)


def run(name: str, now: dt.datetime | None = None,
        open_todos=None, events_range=None) -> tuple[str, bool]:
    """
    执行命令，返回 `(要发的文本, 是否正常)`。

    `ok=False` 只用于**用法错误**（不认识的命令），好让调用方按音量分档
    用"有声"发它（见 docs/TELEGRAM-VOICE.md 的 V2：成功静音、失败有声）。
    **读不到 App 不算用法错误** —— 正文里会如实写 `⚠️`，
    但那仍然是一条正常回执（你问了我答了，只是某处读不到）。

    `open_todos` / `events_range` 可注入（测试用），默认走 `report.py` 的真实读取。
    """
    if name == UNKNOWN:
        return f"❓ 不认识这条命令\n　　{USAGE}", False
    return _listing(now, open_todos, events_range), True


def _listing(now: dt.datetime | None = None,
             open_todos=None, events_range=None) -> str:
    """把"现在的待办与日程"排成一条消息。**只读**，失败如实写出来。"""
    import report

    open_todos = open_todos or report.read_open_todos
    # ⚠️ 一次读**两天**（而不是 read_events 读两次）：每次读取都包含一遍
    # "重复事件扫描"，而那份扫描是全表的（AppleScript 筛不出重复事件）。
    events_range = events_range or report.read_events_between

    now = now or dt.datetime.now()
    today = now.date()
    tomorrow = today + dt.timedelta(days=1)
    wd = "一二三四五六日"[today.isoweekday() - 1]
    errors: list[str] = []
    # ⚠️ 失败必须**单独记标志**，不能只靠"列表是空的"来判断：
    # 空列表有两种含义 —— "真的没有"和"读不到"，而这两者绝不能混。
    # （第一版就踩了：读不到时它照样打印"待办 0 件 🎉 一件都没有"，
    #   正文在撒谎，下面的 ⚠️ 补不回来。日报有一条同源断言。）
    rem_failed = cal_today_failed = cal_tomorrow_failed = False

    # ① 待办：**未完成**的。已完成的由你打钩那一刻就从这份视图里消失了，
    #    这里不需要"哪天完成的"那套判断（那是日报的事）。
    try:
        todos = list(open_todos(today))
    except Exception as e:              # noqa: BLE001
        todos = []
        rem_failed = True
        errors.append(f"提醒事项读不到：{e}")

    # ② 日程：今天**还没结束**的 + 明天全部（一次读取拿两天）。
    #    今天那条按"结束时间"筛，而不是开始时间 —— 正在进行的会议还在进行。
    try:
        _both = list(events_range(today, tomorrow + dt.timedelta(days=1)))
        today_evs = [e for e in _both
                     if e.start.date() == today and (e.end is None or e.end > now)]
        tomorrow_evs = [e for e in _both if e.start.date() == tomorrow]
    except Exception as e:              # noqa: BLE001
        today_evs = []
        tomorrow_evs = []
        cal_today_failed = cal_tomorrow_failed = True
        errors.append(f"日历读不到：{e}")

    lines = [f"📋 现在 {today.month}月{today.day}日 周{wd} {now:%H:%M}", ""]

    # ── 待办
    if rem_failed:
        lines.append("⏳ 待办：读不到（见下方告警）")
    else:
        lines.append(f"⏳ 待办 {len(todos)} 件"
                     + ("　🎉 一件都没有" if not todos else ""))
        for t in todos[:MAX_ITEMS]:
            lines.append(f"　{t.name}")
        if len(todos) > MAX_ITEMS:
            lines.append(f"　…还有 {len(todos) - MAX_ITEMS} 件")
    lines.append("")

    # ── 日程
    for label, evs in (("今天还有", today_evs), ("明天", tomorrow_evs)):
        if not evs:
            continue
        lines.append(f"📅 {label} {len(evs)} 项")
        for e in evs[:MAX_ITEMS]:
            # 重复事件的某一次发生要标出来，否则"明天的跑步"看起来像一次性安排
            rec = (f"（{whens.rrule_text(e.recurrence)}）"
                   if getattr(e, "recurrence", "") else "")
            lines.append(f"　{_when_text(e)} {e.summary}{rec}"
                         + (f"　@{e.location}" if e.location else ""))
        if len(evs) > MAX_ITEMS:
            lines.append(f"　…还有 {len(evs) - MAX_ITEMS} 项")
        lines.append("")
    if not today_evs and not tomorrow_evs:
        # 同样：读不到 ≠ 没有
        if cal_today_failed or cal_tomorrow_failed:
            lines.append("📅 日程：读不到（见下方告警）")
        else:
            lines.append("📅 今明两天都没有日程")
        lines.append("")

    if errors:
        lines.append("⚠️ 以下来源读不到，这份不完整：")
        for e in errors:
            lines.append(f"　{e}")

    return "\n".join(lines).strip()


def _when_text(e) -> str:
    """日程的时间写法：全天就说"全天"，别显示成 00:00。"""
    return "全天" if getattr(e, "all_day", False) else f"{e.start:%H:%M}"


def main() -> int:
    """命令行自检：跑一遍命令（不写入任何东西）。"""
    import argparse

    ap = argparse.ArgumentParser(description="命令层（只读查询）")
    ap.add_argument("text", nargs="?", default="/list", help="你原本会发的那串")
    args = ap.parse_args()

    name = match(args.text)
    if name is None:
        print(f"不是命令（会照常收件）：{args.text!r}")
        return 0
    reply, ok = run(name)
    print(reply)
    print()
    print(f"（ok={ok} → {'静音' if ok else '有声'}发送）")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
