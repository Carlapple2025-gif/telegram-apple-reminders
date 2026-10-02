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


def main() -> int:
    """命令行自测：python3 src/notify.py '标题' '正文' [url]"""
    if len(sys.argv) < 3:
        print("用法：python3 src/notify.py <标题> <正文> [url]")
        print()
        key, source = load_bark_key()
        print(f"当前 key 来源：{source}")
        print(f"key 是否可用：{'是' if key else '否'}")
        return 1

    ok, msg = send_bark(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
