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
    """
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
    args = ap.parse_args()

    if args.status or not args.title:
        key, source = load_bark_key()
        print("通道配置状态：")
        print(f"  Bark:     {'✅ 可用' if key else '❌ 不可用'}  （来源：{source}）")
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
