#!/usr/bin/env python3
"""
通知通道（当前只实现 Bark，够用且已验证）。

设计取舍：
  · **复用 ltc-spider 里已有的 NOTIFY_BARK_KEY**，不复制密钥到本仓库 ——
    少一份密钥副本就少一处泄漏面。要独立配置就用 BARK_KEY 覆盖。
  · 推送失败**不抛异常**，只返回失败原因。理由：日报的价值在于「你看到提醒」，
    如果因为网络抖动让整个定时任务失败退出，反而更糟。调用方决定要不要告警。
  · 支持带 url，手机上点通知可直达备忘录（Bark 的 url 参数）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BARK_ENDPOINT = "https://api.day.app/push"


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def load_bark_key() -> tuple[str | None, str]:
    """
    按优先级取 Bark key，并返回 (key, 来源说明)。

    顺序：环境变量 BARK_KEY → 本仓库 .env → ltc-spider 的 .env（复用已有配置）
    刻意**不把 key 写进本仓库**，避免多一份密钥副本。
    """
    if os.environ.get("BARK_KEY"):
        return os.environ["BARK_KEY"], "环境变量 BARK_KEY"

    local = _read_env_file(ROOT / ".env")
    if local.get("BARK_KEY"):
        return local["BARK_KEY"], "本仓库 .env"

    # 复用已打通的通道。注意这里是**读**，不是复制。
    upstream = Path.home() / "dev" / "ltc-spider" / ".env"
    env = _read_env_file(upstream)
    key = env.get("NOTIFY_BARK_KEY") or env.get("BARK_KEY")
    if key:
        return key, f"复用 {upstream} 里的 NOTIFY_BARK_KEY"

    return None, "未找到 Bark key"


def send_bark(title: str, body: str, url: str | None = None,
               level: str | None = None, group: str = "PDCA") -> tuple[bool, str]:
    """
    发送 Bark 推送。返回 (是否成功, 说明)。

    level="timeSensitive" 可穿透专注模式 —— 验证码那种场景需要，
    日报提醒不需要，所以默认不设。
    """
    if os.environ.get("PDCA_SUPPRESS_SEND"):
        # 测试用安全阀：见 broadcast() 上的说明。
        return False, "已抑制发送（PDCA_SUPPRESS_SEND）"
    key, source = load_bark_key()
    if not key:
        return False, (
            "没有可用的 Bark key。三种配法（任选其一）：\n"
            "  1. export BARK_KEY=xxx\n"
            "  2. 在 pdca/.env 里写 BARK_KEY=xxx\n"
            "  3. 复用 ltc-spider/.env 里的 NOTIFY_BARK_KEY（默认已尝试）"
        )

    payload: dict = {"device_key": key, "title": title, "body": body, "group": group}
    if url:
        payload["url"] = url
    if level:
        payload["level"] = level

    req = urllib.request.Request(
        BARK_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200:
                # Bark 返回 {"code":200,...}，code 非 200 也算业务失败
                try:
                    data = json.loads(raw)
                    if data.get("code") not in (200, None):
                        return False, f"Bark 返回业务错误：{raw[:200]}"
                except json.JSONDecodeError:
                    pass
                return True, f"已推送（key 来源：{source}）"
            return False, f"HTTP {resp.status}：{raw[:200]}"
    except urllib.error.URLError as e:
        return False, f"网络失败：{e}"
    except Exception as e:  # noqa: BLE001
        return False, f"发送失败：{e}"


# ── Telegram 通道
#
# 与 Bark 的分工（刻意分开，理由见 docs/ARCHITECTURE.md）：
#   · Bark   = 强提醒：穿透专注模式，直连无依赖 → 用于"需要你立刻处理"的事
#   · Telegram = 对话与归档：可回看、可搜索、能收你的回复 → 日报/周报
# 所以日报走 Telegram 的同时**保留 Bark**：万一 Telegram 因网络不可用，
# Bark 那条仍能把你叫起来（不让关键路径依赖单一通道）。


def send_telegram(title: str, body: str) -> tuple[bool, str]:
    """发 Telegram 消息。未配置时返回 (False, 说明)，由调用方决定是否在意。"""
    if os.environ.get("PDCA_SUPPRESS_SEND"):
        return False, "已抑制发送（PDCA_SUPPRESS_SEND）"
    try:
        import telegram as tg
    except ImportError as e:
        return False, f"无法加载 telegram 模块：{e}"

    token, chat = tg.load_config()
    if not token or not chat:
        return False, "Telegram 未配置（缺 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID）"

    text = f"{title}\n\n{body}" if title else body
    try:
        r = tg.send(text)
        return True, f"已发送（message_id={r.get('message_id')}）"
    except tg.TelegramError as e:
        return False, f"发送失败：{e}"


def broadcast(title: str, body: str, channels: list[str] | None = None,
              bark_url: str | None = None) -> list[tuple[str, bool, str]]:
    """
    按指定通道发送。返回 [(通道, 是否成功, 说明)]。

    `channels` 默认 ["telegram", "bark"] —— **两个都发**是有意的：
    两个通道互为冗余，任一不可用另一条仍能到达。

    ⚠️ 环境变量 `PDCA_SUPPRESS_SEND` 会让所有发送变成"已抑制"：
    这是给**测试**的安全阀。实测踩到过一次：自检验证看门狗时忘了注入假
    sender，结果两条真告警直接推到了手机上 —— 通知类代码的测试，
    忘一次就是一次真实打扰（而"有没有真的发出去"在断言里看不出来）。
    与 journal 的 `PDCA_JOURNAL_DIR` 同一思路：测试环境由调用方声明。
    """
    if os.environ.get("PDCA_SUPPRESS_SEND"):
        channels = channels or ["telegram", "bark"]
        return [(ch, False, "已抑制发送（PDCA_SUPPRESS_SEND）")
                for ch in channels]

    channels = channels or ["telegram", "bark"]
    results: list[tuple[str, bool, str]] = []

    for ch in channels:
        if ch == "telegram":
            ok, msg = send_telegram(title, body)
        elif ch == "bark":
            ok, msg = send_bark(title, body, url=bark_url)
        else:
            ok, msg = False, f"未知通道：{ch}"
        results.append((ch, ok, msg))

    return results


# ── 心跳（外部监控）
#
# 为什么需要它：上面两个通道解决的是"送没送到"，但**解决不了"根本没跑"**。
# 而"没跑"恰恰是最危险的一种失败 —— 机器睡了、launchd 任务被清了、
# 脚本在推送前就崩了，这三种情况在**本机**全都看不出来（launchctl 里
# 计数还会因 reload 归零，实测 runs=0 / never exited）。
#
# 唯一能发现"没跑"的办法，是让**机器之外**的东西盯着：
# 跑成功就 ping 一下，到点没收到 ping 它就告警。这就是 Healthchecks 一类
# 服务的工作方式（本机只负责"报平安"，判断在远端）。
#
# 隐私约定：**只送计数摘要**，绝不把日报正文（含你的待办内容）传出去。

HEARTBEAT_ENV_KEYS = ("HEALTHCHECK_URL", "HC_PING_URL")


def load_heartbeat_url() -> tuple[str | None, str]:
    """
    取心跳 URL，返回 (url, 来源说明)。

    顺序：环境变量 HEALTHCHECK_URL / HC_PING_URL → 本仓库 .env。
    **刻意不设默认值** —— 没配就是不配，不猜、不静默找一个代替品。
    """
    for key in HEARTBEAT_ENV_KEYS:
        if os.environ.get(key):
            return os.environ[key].strip(), f"环境变量 {key}"

    env = _read_env_file(ROOT / ".env")
    for key in HEARTBEAT_ENV_KEYS:
        if env.get(key):
            return env[key].strip(), f"本仓库 .env 的 {key}"

    return None, "未配置（.env 里没有 HEALTHCHECK_URL；配法见 .env.example）"


def heartbeat_target(url: str, ok: bool) -> str:
    """
    由心跳 URL 推出本次要 POST 的地址（纯函数，便于离线自检）。

    约定来自 Healthchecks 的 Pinging API：
        成功 → 原 URL
        失败 → 原 URL + "/fail"    （主动上报失败，缩短告警延迟）
    """
    base = url.rstrip("/")
    return base if ok else base + "/fail"


def send_heartbeat(ok: bool, summary: str = "",
                   attempts: int = 3) -> tuple[bool, str]:
    """
    给外部心跳服务发一次 ping。返回 (是否成功, 说明)。**永不抛异常**。

    · 未配置 → (False, "未配置…")，不重试、不报错（没配心跳不是故障）。
    · 失败重试 `attempts` 次（默认 3）：心跳丢一次会被远端误判成"任务没跑"，
      所以它比普通推送更值得重试；重试本身无害（远端只是多记一次 ping）。
    · `summary` 只放计数，不放正文 —— 见上面的隐私约定。
    """
    url, source = load_heartbeat_url()
    if not url:
        # 没配是**选择**，不是故障 —— 返回一句短的，别天天在日报里报"警告"。
        # （配法提示留给 --status 与 --heartbeat，那里才是你要看的时候。）
        return False, "未配置（跳过）"

    target = heartbeat_target(url, ok)

    data = summary.encode("utf-8") if summary else b""
    last = "未尝试"
    for i in range(max(1, attempts)):
        req = urllib.request.Request(
            target, data=data,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    tail = "（已上报失败）" if not ok else ""
                    return True, f"已 ping{tail}（来源：{source}）"
                last = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            # 4xx 基本是 URL 配错，重试没有意义 —— 但要说清楚，别静默
            last = f"HTTP {e.code}"
            if 400 <= int(e.code) < 500:
                return False, f"心跳被拒（{last}）：{target}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"

        if i < attempts - 1:
            time.sleep(2 + 3 * i)          # 2s、5s —— 短退避，别拖住定时任务

    return False, f"心跳失败（试了 {attempts} 次）：{last}"


def main() -> int:
    """命令行自测：python3 src/notify.py '标题' '正文' [url]"""
    import argparse

    ap = argparse.ArgumentParser(description="通知通道自测")
    ap.add_argument("title", nargs="?", help="标题")
    ap.add_argument("body", nargs="?", help="正文")
    ap.add_argument("url", nargs="?", help="Bark 的跳转链接（可选）")
    ap.add_argument("--channels", default="telegram,bark",
                    help="要测的通道，逗号分隔（默认 telegram,bark）")
    ap.add_argument("--status", action="store_true", help="只查看配置状态")
    ap.add_argument("--heartbeat", choices=["ok", "fail"],
                    help="只测心跳（ok=报平安，fail=主动上报失败）")
    args = ap.parse_args()

    if args.heartbeat:
        hb, hb_source = load_heartbeat_url()
        if not hb:
            # 提示要指向**能走通的路**，否则等于把人丢在原地
            # （本项目踩过：错误提示指向一个已经删掉的脚本）。
            print(f"❌ {hb_source}")
            print()
            print("怎么配（healthchecks.io 免费档够用，20 个任务）：")
            print("  1. 注册后建一个 check")
            print("  2. Schedule 选 Cron，填     30 21 * * *")
            print("     （与 deploy/com.carl.pdca.report.plist 的 21:30 一致）")
            print("  3. Grace Time 设 1 小时")
            print("     （机器睡过头、唤醒后补跑时不会误报）")
            print("  4. 把它的 ping URL 追加到 pdca/.env：")
            print("     HEALTHCHECK_URL=https://hc-ping.com/xxxxxxxx-xxxx-...")
            print("  5. 在 check 的 Integrations 里接上 Telegram（@HealthchecksBot）")
            print()
            print("配完再跑一次本命令，应回 “已 ping”。")
            return 2
        summary = "手动自测心跳（不含任何日报内容）"
        ok, msg = send_heartbeat(args.heartbeat == "ok", summary=summary)
        print(f"  {'✅' if ok else '❌'} {msg}")
        return 0 if ok else 2

    if args.status or not args.title:
        key, source = load_bark_key()
        print("通道配置状态：")
        print(f"  Bark:     {'✅ 可用' if key else '❌ 不可用'}  （来源：{source}）")
        hb, hb_source = load_heartbeat_url()
        print(f"  心跳:     {'✅ 已配置' if hb else '⚠️ 未配置'}  （来源：{hb_source}）")
        if hb:
            print(f"            成功→{heartbeat_target(hb, True)}")
            print(f"            失败→{heartbeat_target(hb, False)}")
        try:
            import telegram as tg
            token, chat = tg.load_config()
            if token and chat:
                try:
                    me = tg._call(token, "getMe")
                    print(f"  Telegram: ✅ 可用  @{me.get('username')}"
                          f"（chat {chat}）")
                except tg.TelegramError as e:
                    print(f"  Telegram: ⚠️ 有配置但连不上：{e}")
            else:
                print(f"  Telegram: ❌ 未配置"
                      f"（token={'有' if token else '无'} chat={'有' if chat else '无'}）")
        except ImportError:
            print("  Telegram: ❌ 模块不可用")
        if not args.title:
            return 0

    results = broadcast(args.title, args.body or "", 
                        channels=[c.strip() for c in args.channels.split(",") if c.strip()],
                        bark_url=args.url)
    ok_all = True
    for ch, ok, msg in results:
        mark = "✅" if ok else "❌"
        if not ok:
            ok_all = False
        print(f"  {mark} {ch}: {msg}")
    return 0 if ok_all else 2


if __name__ == "__main__":
    sys.exit(main())
