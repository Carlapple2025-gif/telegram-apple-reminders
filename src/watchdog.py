#!/usr/bin/env python3
"""
日报看门狗：在守护进程里"到点检查今天那份日报送出没有"。

## 它补的是哪个洞

Telegram / Bark 两个通道只能证明"送到了"，证明不了"跑了"。日报的失败方式里，
有一类**它自己发不出声**：

    · 21:30 那次进程崩了（推送之前就退出）
    · launchd 任务被清掉 / plist 被改坏
    · 三处 App 的授权失效（这份日报会缺一半内容）

这些都有个共同点：**机器是醒着的，只是日报没跑成**。所以由常驻的守护进程
来盯着就够了 —— 它本来就在跑（`KeepAlive`）。

## 它补不上什么（别把它当万能）

| 情况 | 看门狗 |
|---|---|
| 机器整晚没醒 / 关机 | ❌ 它自己也睡着；醒来时"今天"已经不是那天了 |
| 漏跑好几天 | ❌ 它只管当天；补上的是日报自己的"上一份是几天前"那一行 |
| 外部服务级别的"没收到" | ❌ 那需要一个**机器之外**的东西盯着（Healthchecks 一类） |

分工说清楚：
  · **看门狗** = 当天实时（23:30 还没送到就喊一声）
  · **日报的 gap 行** = 事后对账（漏了几天，恢复时说出来）
  · **外部心跳** = 整机级（可选，见 .env.example 的 HEALTHCHECK_URL）

## 三条设计约束

1. **不加状态文件**。"今天告警过没有"从 journal 的 `digest_missing` 读数里看，
   和"今天送到没有"看 `digest_pushed` 一样 —— 读数是读数，不是状态源。
2. **绝不抛异常**。它跑在守护进程的循环里，一个告警失败绝不能把收件搞停。
   （与 notify 的取舍一致：推送失败只返回原因，由调用方决定怎么办。）
3. **每天最多一次**。凌晨到 23:30 之前不检查，避免"只是晚了"被误报。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent

# 计划的日报时间（与 deploy/io.github.carlapple2025.pdca.report.plist、report.py 一致）
SCHEDULE_HHMM = "21:30"

# 到几点还没送到才算"漏了"。
#
# 为什么不卡在 21:30：launchd 的语义是"睡过了就在唤醒时补跑"，日报本身
# 也可能只是晚了几十分钟。**给出 2 小时宽限**，把"晚"和"没跑"分开 ——
# 误报一次"没送到"，下次你就不会信这条告警了。
WATCH_FROM = dt.time(23, 30)

# 进程内的"今天已经喊过了"备忘。
#
# 为什么 journal 里记了还要这个：**journal 写不进去的时候**（磁盘满、权限被改、
# 目录被挪走），冷却判据就永远读不到"已告警"，于是守护每轮（约 1 秒）都喊一次
# —— 从"漏一条日报"升级成"刷屏轰炸"。这份内存备忘只活在进程里，
# 不是第二份状态（重启后从 journal 恢复），但足以挡住那种失控。
_ALERTED_IN_PROCESS: set = set()


def pending(now: dt.datetime | None = None) -> tuple[bool, str]:
    """
    该不该告警？返回 (是否告警, 原因说明)。**纯判断，不发通知。**

    判据三条，缺一不可：
      ① 已经过了 WATCH_FROM
      ② 今天**没有**投递成功的读数
      ③ 今天还没告警过（每天一次）
    """
    now = now or dt.datetime.now()
    today = now.date().isoformat()

    import journal

    if now.time() < WATCH_FROM:
        return False, f"还没到 {WATCH_FROM.strftime('%H:%M')}，先不打扰"

    if journal.delivered_on(today):
        return False, "今天已投递成功"

    if journal.alerted_on(today) or today in _ALERTED_IN_PROCESS:
        return False, "今天已经告警过（每天最多一次）"

    return True, f"{today} 到 {now.strftime('%H:%M')} 仍无投递成功的读数"


def alert_text(now: dt.datetime | None = None) -> tuple[str, str]:
    """告警的 (标题, 正文)。正文要给**能走通的路**，不能只说"出错了"。"""
    now = now or dt.datetime.now()
    day = now.date().isoformat()
    title = "⚠️ 今天的日报没送到"
    body = "\n".join([
        f"{day} 的日报到 {now.strftime('%H:%M')} 还没有送出（计划 {SCHEDULE_HHMM}）。",
        "",
        "可能的原因：进程崩在推送之前 / 定时任务被清 / 授权失效。",
        "",
        "手动补一份（会正常推送）：",
        "　cd " + str(ROOT),
        "　python3 src/report.py",
        "",
        "本提示每天最多一次；漏跑的天数会在下一份日报里说明。",
    ])
    return title, body


def alert_once(now: dt.datetime | None = None,
               sender: Callable[..., list] | None = None) -> tuple[bool, str]:
    """
    检查并发一次告警（若该发）。返回 (是否发了, 说明)。**永不抛异常。**

    `sender` 可注入（测试用），签名与 notify.broadcast 一致。
    """
    try:
        should, why = pending(now)
    except Exception as e:  # noqa: BLE001 —— 读数读不到不能拖垮守护
        return False, f"检查失败（不影响收件）：{type(e).__name__}: {e}"

    if not should:
        return False, why

    now = now or dt.datetime.now()
    day = now.date().isoformat()
    title, body = alert_text(now)

    # 先登记、再发送：万一发送或记账卡住/抛错，也不会在同一轮里被反复触发。
    _ALERTED_IN_PROCESS.add(day)

    ok, detail = False, "未发送"
    try:
        if sender is None:
            import notify
            sender = notify.broadcast
        results = sender(title, body)
        ok = any(r[1] for r in results)
        detail = "；".join(f"{r[0]}{'✓' if r[1] else '✗'}" for r in results)
    except Exception as e:  # noqa: BLE001
        detail = f"发送异常：{type(e).__name__}: {e}"

    # 无论发没发成都要记：发失败时若不记，下一轮（1 秒后）会再试一次，
    # 变成刷屏；记了就是"每天一次"，失败也在日报/日志里看得见。
    try:
        import journal
        journal.log_digest_missing(day, detail=detail,
                                   channel="bark+telegram" if ok else "")
    except Exception as e:  # noqa: BLE001
        return ok, f"{detail}（另外：告警记录没写进 journal：{e}）"

    return ok, detail


def tick(now: dt.datetime | None = None) -> None:
    """
    守护进程每轮调用一次。**这是给常驻进程用的入口**：
    吞掉一切异常、不返回状态 —— 看门狗出问题不该影响收件（它的唯一职责）。
    """
    try:
        fired, why = alert_once(now)
    except Exception as e:  # noqa: BLE001 —— 双保险，绝不冒到守护的主循环
        print(f"[watchdog] 异常已忽略：{type(e).__name__}: {e}", file=sys.stderr)
        return
    if fired:
        print(f"[watchdog] 已发告警：{why}", file=sys.stderr)


def main() -> int:
    """命令行：python3 src/watchdog.py --check（只看判断，不发通知）"""
    import argparse

    ap = argparse.ArgumentParser(description="日报看门狗（判断今天送出没有）")
    ap.add_argument("--check", action="store_true",
                    help="只打印判断结果，不发通知")
    ap.add_argument("--force", action="store_true",
                    help="忽略时间与冷却，真的发一次（验证通道用）")
    args = ap.parse_args()

    now = dt.datetime.now()
    if args.force:
        title, body = alert_text(now)
        print(title)
        print("─" * 40)
        print(body)
        print("─" * 40)
        ok, detail = alert_once(now=now,
                                sender=None if not args.check else
                                (lambda t, b: [("dry-run", True, "未发送")]))
        print(f"{'✅' if ok else '·'} {detail}")
        return 0

    should, why = pending(now)
    mark = "⚠️ 该告警" if should else "· 不告警"
    print(f"{mark}：{why}")
    print(f"  计划时间 {SCHEDULE_HHMM}，{WATCH_FROM.strftime('%H:%M')} 之后才算漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
