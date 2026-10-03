#!/usr/bin/env python3
"""
提醒事项访问层。

为什么待办放在这里而不是备忘录：
    实测备忘录写入时会剥掉 class/data 属性，清单项的勾选状态**根本不存在于
    HTML 里** —— 代码既读不到也写不了。而提醒事项的脚本字典里
    `completed` / `completion date` / `due date` 都是**可读写**的（已实测 8/8）。

设计原则（沿用 notes.py 的经验）：
  1. **写入后必须读回验证** —— AppleScript 会"不报错但没生效"
  2. **判定逻辑留在 Python**，AppleScript 只接受具体 id/字符串 ——
     在 bash/AppleScript 里拼条件表达式反复因引号转义出错
  3. **所有操作限定在我们自己的列表**（配置里指定），不碰用户其它列表
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RemindersError(RuntimeError):
    pass


def lit(s: str) -> str:
    """转成 AppleScript 字符串字面量。反斜杠必须最先转义。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# AppleEvent 超时（-1712）值得重试。原因很实际：launchd 在早上 07:00 触发时，
# 提醒事项 App 很可能**没在运行** —— 首次调用要等它冷启动，容易超时。
# 实测踩到过：07:00 那次顺延报 "-1712 AppleEvent 逾时"，只好退化成读备忘录标记。
#
# 只对超时重试，不对权限错误重试 —— 权限问题重试多少次都一样，
# 只会白白拖慢流程。
RETRYABLE = ("-1712", "AppleEvent", "timed out", "逾时", "逾時")


def run(source: str, retries: int = 3, backoff: float = 1.5) -> str:
    """执行 AppleScript。超时可重试，权限错误立即抛出。"""
    import time as _time
    last_err = ""
    for attempt in range(1, retries + 1):
        proc = subprocess.run(["osascript", "-e", source],
                              capture_output=True, text=True)
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        if not err:
            return out

        if "-10004" in err or "privilege violation" in err:
            raise RemindersError(
                "提醒事项拒绝访问（-10004 越权）。\n"
                "请到 系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「提醒事项」"
            )
        if "-1743" in err:
            raise RemindersError(
                "系统不允许向「提醒事项」发送 Apple 事件（-1743）。同上需授权。"
            )

        last_err = err
        if any(k in err for k in RETRYABLE) and attempt < retries:
            # 顺便"唤醒"一下提醒事项，让它的冷启动发生在重试之前
            subprocess.run(["osascript", "-e", 'tell application "Reminders" to activate'],
                           capture_output=True)
            _time.sleep(backoff * attempt)
            continue
        raise RemindersError(f"AppleScript 失败：{err}")

    raise RemindersError(f"AppleScript 连续 {retries} 次失败：{last_err}")


@dataclass
class Reminder:
    id: str
    name: str
    completed: bool
    body: str
    due: str  # 原始字符串形式，够用即可


class Reminders:
    """
    限定在某个列表内的提醒事项访问。

    列表名由 config.json 的 reminders_list 指定；不存在时会**明确报错**
    而不是默默用别的列表 —— 写错列表会让待办混进用户真实数据。
    """

    def __init__(self, config: dict | None = None):
        if config is None:
            p = ROOT / "config.json"
            if not p.is_file():
                raise RemindersError(f"缺少 {p}，请先运行 bash deploy/init.sh")
            config = json.loads(p.read_text(encoding="utf-8"))
        self.config = config
        self.list_name = config.get("reminders_list", "PDCA")

    # ── 列表

    def list_names(self) -> list[str]:
        out = run('tell application "Reminders" to get name of every list')
        return [n.strip() for n in out.split(",") if n.strip()]

    def ensure_list(self) -> bool:
        """确保列表存在。返回是否**新建**了它。"""
        if self.list_name in self.list_names():
            return False
        run(
            'tell application "Reminders"\n'
            f'  make new list with properties {{name:{lit(self.list_name)}}}\n'
            '  return "ok"\n'
            'end tell'
        )
        if self.list_name not in self.list_names():
            raise RemindersError(f"创建列表「{self.list_name}」失败")
        return True

    def verify_list(self) -> int:
        """确认列表存在并返回条数。"""
        if self.list_name not in self.list_names():
            raise RemindersError(
                f"列表「{self.list_name}」不存在。\n"
                f"先运行 bash deploy/init.sh 创建，或在提醒事项里手动建一个同名列表。"
            )
        out = run(
            'tell application "Reminders"\n'
            f'  return (count of reminders of list {lit(self.list_name)}) as string\n'
            'end tell'
        )
        return int(out.strip() or 0)

    # ── 读

    def all_reminders(self) -> list[Reminder]:
        """
        列出该列表内全部条目。

        输出格式：每条三行（id / completed / name），外加 body 与 due。
        用「一行一个字段」而不是分隔符拼接 —— AppleScript 字面量里
        不能可靠嵌入控制字符（notes.py 踩过这个坑）。
        """
        out = run(
            'tell application "Reminders"\n'
            f'  set L to list {lit(self.list_name)}\n'
            '  set out to ""\n'
            '  repeat with r in (every reminder of L)\n'
            '    set out to out & (id of r) & linefeed\n'
            '    set out to out & (completed of r as string) & linefeed\n'
            '    set out to out & (name of r) & linefeed\n'
            '    set out to out & (body of r) & linefeed\n'
            '    set out to out & "----" & linefeed\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        )
        items: list[Reminder] = []
        chunk: list[str] = []
        for line in out.splitlines():
            if line.strip() == "----":
                if len(chunk) >= 3:
                    items.append(Reminder(
                        id=chunk[0],
                        completed=chunk[1].strip().lower() == "true",
                        name=chunk[2],
                        body=(chunk[3] if len(chunk) > 3 else ""),
                        due="",
                    ))
                chunk = []
            else:
                chunk.append(line)
        return items

    def open_reminders(self) -> list[Reminder]:
        return [r for r in self.all_reminders() if not r.completed]

    # ── 写（全部带读回验证）

    def create(self, name: str, body: str = "", due: str | None = None) -> Reminder:
        """
        新建条目。`due` 用 AppleScript 的相对时间表达式（如 "1 * days"）。

        读回验证用 **id 差集**，不按名字 —— 按名字可能撞车（notes.py 的教训）。
        """
        before = {r.id for r in self.all_reminders()}
        due_clause = f", due date:(current date) + {due}" if due else ""
        run(
            'tell application "Reminders"\n'
            f'  set L to list {lit(self.list_name)}\n'
            f'  make new reminder at L with properties '
            f'{{name:{lit(name)}, body:{lit(body)}{due_clause}}}\n'
            '  return "ok"\n'
            'end tell'
        )
        fresh = [r for r in self.all_reminders() if r.id not in before]
        if not fresh:
            raise RemindersError(
                f"新建「{name}」后 id 集合没有新增 —— 写入未生效。"
            )
        return fresh[0]

    def set_completed(self, reminder_id: str, completed: bool = True) -> Reminder:
        """设置完成状态，并读回验证。"""
        run(
            'tell application "Reminders"\n'
            f'  set hits to (every reminder whose id is {lit(reminder_id)})\n'
            '  if (count of hits) is 0 then return "NOTFOUND"\n'
            f'  set completed of item 1 of hits to {"true" if completed else "false"}\n'
            '  return "ok"\n'
            'end tell'
        )
        for r in self.all_reminders():
            if r.id == reminder_id:
                if r.completed != completed:
                    raise RemindersError(
                        f"设置完成状态后读回不一致：期望 {completed}，实际 {r.completed}"
                    )
                return r
        raise RemindersError(f"设置完成状态后找不到条目 {reminder_id}")

    def delete(self, reminder_id: str) -> bool:
        run(
            'tell application "Reminders"\n'
            f'  set hits to (every reminder whose id is {lit(reminder_id)})\n'
            '  if (count of hits) is 0 then return "NOTFOUND"\n'
            '  delete item 1 of hits\n'
            '  return "ok"\n'
            'end tell'
        )
        return reminder_id not in {r.id for r in self.all_reminders()}


# ── 去重键
#
# 同步必须**幂等**：这个任务每天跑，判重不准会让提醒事项越积越多。
#
# 键的构成：当天页的 note_id + 行号 + 归一化正文。
#   · 带 note_id/行号 → 同一天的同一行永远对应同一条待办
#   · 带归一化正文   → 你在同一天同一行改了文字，会被识别为"新的内容"
# 之所以不用正文单独做键：不同天可能出现同名待办（"交电费"每月都有），
# 那不算重复。
#
# 标记存在 body 里（提醒事项的 body 可读写），格式 `pdca:<key>`。

KEY_PREFIX = "pdca:"


def make_key(note_id: str, line_no: int, text: str) -> str:
    import hashlib
    h = hashlib.sha256(f"{note_id}|{line_no}|{text}".encode("utf-8")).hexdigest()[:16]
    return f"{KEY_PREFIX}{h}"


def key_of(reminder: Reminder) -> str | None:
    """从 body 里取出去重键。"""
    if reminder.body and reminder.body.startswith(KEY_PREFIX):
        return reminder.body.split()[0].strip()
    return None


# ── CLI

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="提醒事项访问（限定 PDCA 列表）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="确认列表存在并列出条数")
    sub.add_parser("list", help="列出该列表全部条目（含完成状态）")
    sub.add_parser("lists", help="列出所有列表名")

    args = ap.parse_args()

    try:
        r = Reminders()
        if args.cmd == "lists":
            for n in r.list_names():
                mark = " ← 本工具使用" if n == r.list_name else ""
                print(f"  · {n}{mark}")
            return 0
        if args.cmd == "verify":
            count = r.verify_list()
            print(f"✅ 列表「{r.list_name}」有效，{count} 条")
            return 0
        if args.cmd == "list":
            items = r.all_reminders()
            print(f"列表「{r.list_name}」共 {len(items)} 条：")
            for it in items:
                mark = "☑" if it.completed else "☐"
                print(f"  {mark} {it.name}")
                k = key_of(it)
                if k:
                    print(f"      {k}")
            return 0
    except RemindersError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
