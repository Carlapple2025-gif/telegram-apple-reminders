#!/usr/bin/env python3
"""
Telegram 通道：双向。

为什么需要它（备忘录和提醒事项都做不到的事）：
    agent 在**定时批处理**里运行，没法提问。于是「判断权交给用户」这条原则
    里"遇到歧义就问一句"的环节一直落不了地 —— 只能把疑问标记进留档，
    等你哪天看到。Telegram 是唯一能双向的通道，补上了这个缺口。

分工（不让关键路径依赖单一通道）：
    · 强提醒（需要你立刻处理）→ Bark：穿透专注模式，且直连无依赖
    · 日报 / 周报 / 归档      → Telegram：可回看、可搜索
    · agent 的提问            → Telegram：只有它能收到你的回复

配置：**独立 bot，不与其它项目共用**（用户明确要求）。
    token 放本仓库 .env（已被 .gitignore 排除，0600）：
        TELEGRAM_BOT_TOKEN=...
        TELEGRAM_CHAT_ID=...
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


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


def load_config() -> tuple[str, str]:
    """
    取 (token, chat_id)。

    优先级：环境变量 → 本仓库 .env。
    **刻意不回退到其它项目的 bot** —— 用户要求独立 bot，
    混用会让日志、权限、归档纠缠在一起。
    """
    env = _read_env_file(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or ""
    chat = os.environ.get("TELEGRAM_CHAT_ID") or env.get("TELEGRAM_CHAT_ID") or ""
    return token.strip(), chat.strip()


def _call(token: str, method: str, params: dict | None = None,
          timeout: int = 20) -> dict:
    url = f"{API}/bot{token}/{method}"
    data = None
    if params:
        data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise TelegramError(f"HTTP {e.code}：{body}") from None
    except urllib.error.URLError as e:
        raise TelegramError(
            f"网络失败：{e}\n"
            f"（实测 Mac mini 可直连 api.telegram.org；若失败先确认网络）"
        ) from None

    if not payload.get("ok"):
        raise TelegramError(f"API 返回错误：{payload.get('description', payload)}")
    return payload.get("result") or {}


# ── 发送

def send(text: str, chat_id: str | None = None,
         disable_notification: bool = False,
         parse_mode: str | None = None) -> dict:
    """
    发消息。返回 Telegram 的 result（含 message_id）。

    不做自动重试：调用方能看到失败并决定怎么处理。
    静默重试会让"没发出去"变成"以为发出去了"。
    """
    token, cfg_chat = load_config()
    if not token:
        raise TelegramError(
            "没有 Telegram token。请创建独立 bot 并写进 .env：\n"
            f"  cp {ROOT}/.env.example {ROOT}/.env\n"
            f"  在 .env 里填 TELEGRAM_BOT_TOKEN=...（找 @BotFather 创建）\n"
            f"  然后 chmod 600 {ROOT}/.env"
        )
    chat = chat_id or cfg_chat
    if not chat:
        raise TelegramError(
            "没有 chat_id。先给 bot 发一条消息，然后运行：\n"
            "  python3 src/telegram.py whoami   ← 会自动查出并提示如何填写"
        )

    params: dict = {"chat_id": chat, "text": text}
    if disable_notification:
        params["disable_notification"] = True
    if parse_mode:
        params["parse_mode"] = parse_mode
    return _call(token, "sendMessage", params)


# ── 接收

def get_updates(offset: int | None = None, timeout: int = 0,
                limit: int = 10) -> list[dict]:
    token, _ = load_config()
    if not token:
        raise TelegramError("没有 Telegram token（见 src/telegram.py 顶部说明）")
    params: dict = {"limit": limit}
    if offset is not None:
        params["offset"] = offset
    if timeout:
        params["timeout"] = timeout
    result = _call(token, "getUpdates", params, timeout=max(20, timeout + 10))
    return result if isinstance(result, list) else []


def wait_for_reply(chat_id: str | None = None, timeout_sec: int = 300,
                   prompt: str | None = None,
                   poll_interval: int = 3) -> str | None:
    """
    发一条提问，然后等回复。返回回复文本；超时返回 None。

    实现要点：
      · 先用 getUpdates 把历史更新消费掉（取最大 update_id + 1 作为 offset），
        否则会把几天前的旧消息当成这次的回答 —— 那会导致**答非所问**，
        而且完全看不出来。
      · 超时返回 None 而不是抛异常：调用方设计上"不回复就走默认"，
        提问失败不该阻塞整条流程。
    """
    token, cfg_chat = load_config()
    chat = chat_id or cfg_chat
    if not chat:
        raise TelegramError("没有 chat_id，无法提问")

    # 1. 消费掉历史，定位到"现在"
    try:
        history = get_updates(limit=100)
        offset = (max(u["update_id"] for u in history) + 1) if history else None
    except TelegramError:
        offset = None

    # 2. 提问
    if prompt:
        send(prompt, chat_id=chat)

    # 3. 轮询等待
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            updates = get_updates(offset=offset, timeout=poll_interval)
        except TelegramError:
            time.sleep(poll_interval)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            if text and (not chat or str(msg.get("chat", {}).get("id")) == str(chat)):
                return text
        time.sleep(poll_interval)

    return None


# ── CLI

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Telegram 通道（pdca 专用 bot）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="检查 token / chat_id 是否配好")
    sub.add_parser("whoami", help="查出你的 chat_id（需先给 bot 发过消息）")
    sub.add_parser("updates", help="列出最近的更新（排查用）")

    p_send = sub.add_parser("send", help="发一条测试消息")
    p_send.add_argument("text")

    p_wait = sub.add_parser("wait", help="发提问并等待回复（100 秒）")
    p_wait.add_argument("prompt")

    args = ap.parse_args()

    try:
        if args.cmd == "status":
            token, chat = load_config()
            print(f"  token:   {'已配置' if token else '❌ 未配置'}"
                  f"{f'（{len(token)} 字符）' if token else ''}")
            print(f"  chat_id: {chat or '❌ 未配置'}")
            if token:
                me = _call(token, "getMe")
                print(f"  bot:     @{me.get('username')}（{me.get('first_name')}）")
                print(f"  ⚠️ 确认这是 pdca 专用 bot，不是其它项目在用的")
            return 0 if (token and chat) else 1

        if args.cmd == "whoami":
            token, _ = load_config()
            if not token:
                print("❌ 先配置 TELEGRAM_BOT_TOKEN（见 .env.example）", file=sys.stderr)
                return 1
            me = _call(token, "getMe")
            print(f"bot: @{me.get('username')}")
            updates = get_updates(limit=20)
            if not updates:
                print()
                print("没有收到任何消息。请：")
                print(f"  1. 在 Telegram 里打开 @{me.get('username')}")
                print("  2. 给它发一条任意消息（比如 hi）")
                print("  3. 再运行一次本命令")
                return 1
            seen = {}
            for u in updates:
                msg = u.get("message") or {}
                c = msg.get("chat") or {}
                if c.get("id"):
                    seen[c["id"]] = c.get("first_name") or c.get("title") or "?"
            print()
            print("找到以下 chat：")
            for cid, name in seen.items():
                print(f"  {cid}  （{name}）")
            if len(seen) == 1:
                cid = next(iter(seen))
                print()
                print("把下面这行加进 .env（然后 chmod 600 .env）：")
                print(f"  TELEGRAM_CHAT_ID={cid}")
            return 0

        if args.cmd == "updates":
            for u in get_updates(limit=10):
                msg = u.get("message") or {}
                who = (msg.get("from") or {}).get("first_name", "?")
                print(f"  [{u['update_id']}] {who}: {(msg.get('text') or '(非文本)')[:60]}")
            return 0

        if args.cmd == "send":
            r = send(args.text)
            print(f"✅ 已发送 message_id={r.get('message_id')}")
            return 0

        if args.cmd == "wait":
            print(f"提问：{args.prompt}")
            print("（等你回复，最多 100 秒）")
            reply = wait_for_reply(timeout_sec=100, prompt=args.prompt)
            if reply is None:
                print("⏱️ 超时未收到回复（按设计，调用方此时走默认值）")
                return 1
            print(f"✅ 收到回复：{reply}")
            return 0

    except TelegramError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
