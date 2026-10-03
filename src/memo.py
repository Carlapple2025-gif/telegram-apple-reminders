#!/usr/bin/env python3
"""
Apple 备忘录：只读快照 + 单向追加。

## 只做两件事

| 操作 | 方向 | 用途 |
|---|---|---|
| `snapshot()` | 备忘录 → agent | **只读**，用于判断"我提交的备忘还在不在" |
| `add()` | agent → 备忘录 | **单向追加**，写完不读回做判断 |

**没有删除、没有修改、没有"以自己为准重建"。**
删除权限完全在用户手里（见 docs/ARCHITECTURE.md 权限矩阵）。

## 为什么这么克制

v1 曾让 agent 读写备忘录正文、按行替换、删除条目，结果是：
  · agent 需要判断"这两条是不是同一个" → 猜身份 → 四次同源 bug
  · agent 写的东西又变成自己的输入 → 自指 → 用户形容为"左脚踩右脚"
  · 用户删掉一条，agent 可能凭副本把它造回来

v4 的解法：**agent 只往里"增"，从不以自己为准去改或删。**
"谁还在"永远由只读快照回答 —— 这一条消掉了上面整类问题。

## 一条备忘 = 一条笔记

不用"一个笔记里多行"，因为：
  · 每条有独立的 Apple 笔记 id，天然可作为标识（**不需要自己造 id 标记**）
  · 不受"备忘录会合并多个列表 / 剥离属性"那些解析坑影响
  · 用户删除单条就是删一条笔记，语义干净

## 实测踩过的坑（都已在代码里规避）

  · `every note` 与 `count of notes` **包含「最近删除」**，且它排在最前
    → 所有查询必须限定在指定文件夹内
  · 文件夹要用 **id** 定位，不能用名字（名字可重复、可改）
  · 写入可能**静默失败**（不报错但没生效）→ 写完必须读回验证
  · `whose name is` 返回 0 条 → 必须自己遍历比对
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"

# AppleScript 里各条记录之间的分隔符。
#
# 分隔符用 chr(1) / chr(2)：笔记标题里可能出现任何**可打印**字符，
# 但控制字符不可能出现在用户文本里 —— 拼接结果无歧义。
#
# ⚠️ 这里有两个都踩过的坑（写下来免得再犯）：
#   ① AppleScript **不支持 \u 转义** —— 写 "\u0001" 会直接编译失败
#      （Expected """ but found unknown token，-2741）
#   ② 也不能把**裸控制字节**拼进 AppleScript 源码 —— 同样编译失败
# 正解：在 AppleScript 里用 `character id 1` 构造（见 run_applescript 的
# 调用处，源码里有 `set FS to character id 1`）。
# 下面两个常量只用于 **Python 侧解析**。
FSEP = chr(1)     # 字段分隔
RSEP = chr(2)     # 记录分隔


class MemoError(Exception):
    """备忘录操作失败（带可操作的提示）。"""


# ── AppleScript 通道
#
# 用 osascript 子进程而不是 PyObjC / ScriptingBridge，原因见
# docs/ARCHITECTURE.md：TCC 授权绑定到**调用进程身份**，
# 而 Terminal 已经有稳定的自动化授权。走子进程可以复用那份授权。

def _as_literal(s: str) -> str:
    """转成 AppleScript 字符串字面量。反斜杠必须先转义（否则会吃掉后续引号）。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_applescript(src: str, timeout: int = 30) -> str:
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MemoError("AppleScript 超时（备忘录可能在冷启动，稍后重试）") from None

    err = p.stderr.strip()
    if err:
        if "-10004" in err or "privilege violation" in err:
            raise MemoError(
                "备忘录拒绝访问（-10004 越权）。请到 "
                "系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「备忘录」")
        if "-1743" in err:
            raise MemoError("系统不允许向备忘录发送 Apple 事件（-1743），同上需授权")
        if "-1728" in err:
            raise MemoError(f"对象不存在（-1728）：{err}")
        raise MemoError(f"AppleScript 失败：{err}")
    return p.stdout.strip()


def folder_id() -> str:
    """读 config.json 里的备忘文件夹 id。"""
    if not CONFIG.is_file():
        raise MemoError(
            "缺少配置文件 " + str(CONFIG) + "\n"
            "请先运行：bash deploy/setup-v4.sh --apply")
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise MemoError(f"config.json 格式错误：{e}") from None

    fid = (cfg.get("memo_folder_id") or cfg.get("folder_id") or "").strip()
    if not fid:
        raise MemoError("config.json 里没有 memo_folder_id（备忘文件夹未初始化）")
    return fid


def active_folder_id() -> str:
    """当前使用的文件夹 id（memo_folder_id 优先，回退到 v1 的 folder_id）。"""
    return folder_id()


# ── 数据模型

@dataclass
class Memo:
    """备忘录里的一条备忘。"""
    note_id: str          # Apple 笔记 id（天然标识，无需自造）
    name: str             # 标题
    body: str = ""        # 正文（纯文本）

    @property
    def display(self) -> str:
        """给人看的一行。"""
        text = (self.body or self.name or "").strip()
        return text.splitlines()[0] if text else "(空)"


# ── 只读快照

def snapshot() -> list[Memo]:
    """
    读取当前文件夹里**所有**备忘（只读）。

    这是"谁还在"的唯一依据 —— 用户删掉的不会出现在这里，
    所以调用方天然不会去提醒一条已处理的事。

    只取 id 与 name（正文另按需读，减少一次往返的开销）。
    """
    fid = active_folder_id()
    out = run_applescript(
        'tell application "Notes"\n'
        f'  set targetFolder to folder id {_as_literal(fid)}\n'
        '  set out to ""\n'
        '  set FS to character id 1\n'
        '  set RS to character id 2\n'
        '  repeat with n in notes of targetFolder\n'
        f'    set out to out & (id of n) & FS & (name of n) & RS\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')
    return parse_snapshot(out)


def parse_snapshot(raw: str) -> list[Memo]:
    """
    解析快照输出。抽成独立函数便于离线测试（不碰 AppleScript）。

    格式：每条 `<id>\\u0001<name>\\u0002`。
    用不可见字符做分隔符而不是换行/竖线 —— 笔记标题里可能有换行或竖线，
    而 \\u0001 / \\u0002 不可能出现在标题中。
    """
    memos: list[Memo] = []
    for chunk in raw.split("\u0002"):
        chunk = chunk.strip("\n\r")
        if not chunk or "\u0001" not in chunk:
            continue
        nid, name = chunk.split("\u0001", 1)
        nid, name = nid.strip(), name.strip()
        if nid:
            memos.append(Memo(note_id=nid, name=name))
    return memos


def snapshot_ids() -> set[str]:
    """只取 id 集合 —— 判断"我提交的还在不在"时够用，且最快。"""
    return {m.note_id for m in snapshot()}


def text_of(note_id: str) -> str:
    """
    读一条备忘的正文（只读）。

    用遍历比对而不是 `whose id is`：实测 `whose name is` 会返回 0 条，
    id 虽然能命中，但为一致性仍走遍历（条目数很少，代价可忽略）。
    """
    fid = active_folder_id()
    out = run_applescript(
        'tell application "Notes"\n'
        f'  set targetFolder to folder id {_as_literal(fid)}\n'
        '  repeat with n in notes of targetFolder\n'
        f'    if (id of n) is {_as_literal(note_id)} then return plaintext of n\n'
        '  end repeat\n'
        '  return "__NOT_FOUND__"\n'
        'end tell')
    if out == "__NOT_FOUND__":
        raise MemoError(f"备忘不存在（可能已被删除）：{note_id}")
    return out


# ── 单向追加

def add(text: str, title: str | None = None) -> Memo:
    """
    新建一条备忘，返回它（含 Apple 分配的 note_id）。

    **写完读回验证**：备忘录的写入会静默失败（不报错但没生效），
    所以必须用"写入前后的 id 差集"确认 —— 不能靠标题比对，
    因为备忘录会规范化标题（截断、去首尾空白）。

    调用方拿到 note_id 后应记进 journal（`journal.log_memo`），
    那是观察阶段判断"这条还在不在"的凭据。
    """
    text = (text or "").strip()
    if not text:
        raise MemoError("备忘内容为空")

    fid = active_folder_id()

    # ① 记录写入前的 id 集合
    before = snapshot_ids()

    # ② 建笔记。标题用首行，正文用全文（HTML，每行一个 <div>，
    #    否则多行会被折叠成一行 —— 实测结论）
    first_line = text.splitlines()[0][:120]
    name = (title or first_line).strip()
    body_html = "".join(
        f"<div>{_html_escape(line)}</div>" for line in text.splitlines() or [""]
    )

    run_applescript(
        'tell application "Notes"\n'
        f'  set targetFolder to folder id {_as_literal(fid)}\n'
        f'  make new note at targetFolder with properties '
        f'{{name:{_as_literal(name)}, body:{_as_literal(body_html)}}}\n'
        '  return "ok"\n'
        'end tell')

    # ③ 读回验证：用 **id 差集** 定位新笔记
    #    （不用标题比对 —— 标题会被规范化，比对不可靠）
    after = snapshot_ids()
    new_ids = after - before
    if len(new_ids) != 1:
        raise MemoError(
            f"写入未生效或产生了 {len(new_ids)} 条新笔记（预期 1 条）。"
            f"内容：{text[:40]!r}")

    note_id = next(iter(new_ids))
    return Memo(note_id=note_id, name=name, body=text)


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── 删除（**仅供"改分类"使用**）
#
# ⚠️ 这违反了 v4 最初的约束"agent 不删任何东西"。之所以开这个口子：
# 用户明确需要把记错类型的东西改过来（实测："改为备忘录"），
# 而"改分类"必然要把原条从旧 App 移走。
#
# 为了让这个能力**不被滥用**，限制成：
#   · 只按 note_id 删（不按标题/内容模糊匹配 —— 绝不猜）
#   · 只由 reclassify 调用（其他地方不用）
#   · 每次删除都记进 journal，可追溯

def delete(note_id: str) -> bool:
    """
    按 id 删除一条备忘（移到系统「最近删除」，30 天内可恢复）。
    返回是否删除成功（读回验证）。
    """
    if not note_id:
        raise MemoError("删除需要明确的 note_id（不接受模糊匹配）")

    fid = active_folder_id()
    run_applescript(
        'tell application "Notes"\n'
        f'  set targetFolder to folder id {_as_literal(fid)}\n'
        '  repeat with n in notes of targetFolder\n'
        f'    if (id of n) is {_as_literal(note_id)} then\n'
        '      delete n\n'
        '      return "ok"\n'
        '    end if\n'
        '  end repeat\n'
        '  return "NOTFOUND"\n'
        'end tell')

    # 读回验证：备忘录删除也可能静默失败
    return note_id not in snapshot_ids()


# ── 初始化：新建并使用一个备忘文件夹
#
# 为什么不复用 v1 的 logs 文件夹：那里有用户亲手写的当天页，
# 属于用户数据。新设计用新文件夹，职责清晰，也不动用户已有的东西。

def ensure_folder(name: str = "备忘") -> tuple[str, str, bool]:
    """
    确保存在指定名字的文件夹，返回 (folder_id, name, 是否新建)。

    同名文件夹已存在则直接复用（**不新建第二个** —— 名字可重复，
    所以必须自己遍历比对，不能靠 AppleScript 的 whose）。
    """
    folders = list_folders()
    for fid, fname in folders:
        if fname == name:
            return fid, fname, False

    run_applescript(
        'tell application "Notes"\n'
        f'  make new folder with properties {{name:{_as_literal(name)}}}\n'
        '  return "ok"\n'
        'end tell')

    # 读回确认（同样防静默失败）
    for fid, fname in list_folders():
        if fname == name:
            return fid, fname, True
    raise MemoError(f"新建文件夹失败（读回时找不到 {name!r}）")


def list_folders() -> list[tuple[str, str]]:
    """列出所有文件夹 (id, name)。只读。"""
    out = run_applescript(
        'tell application "Notes"\n'
        '  set out to ""\n'
        '  set FS to character id 1\n'
        '  set RS to character id 2\n'
        '  repeat with f in folders\n'
        f'    set out to out & (id of f) & FS & (name of f) & RS\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')
    pairs: list[tuple[str, str]] = []
    for chunk in out.split("\u0002"):
        chunk = chunk.strip("\n\r")
        if "\u0001" not in chunk:
            continue
        fid, fname = chunk.split("\u0001", 1)
        if fid.strip():
            pairs.append((fid.strip(), fname.strip()))
    return pairs


def save_folder_to_config(fid: str, name: str) -> None:
    """把备忘文件夹写进 config.json（保留其它字段）。"""
    cfg: dict = {}
    if CONFIG.is_file():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    cfg["memo_folder_id"] = fid
    cfg["memo_folder_name"] = name
    import datetime as _dt
    cfg["memo_updated_at"] = _dt.datetime.now().astimezone().replace(
        microsecond=0).isoformat()
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                      encoding="utf-8")


# ── CLI

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Apple 备忘录（只读快照 + 单向追加）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出当前文件夹里的备忘（只读）")
    sub.add_parser("folders", help="列出所有文件夹（只读）")

    p_init = sub.add_parser("init", help="新建并启用备忘文件夹")
    p_init.add_argument("--name", default="备忘", help="文件夹名（默认：备忘）")
    p_init.add_argument("--apply", action="store_true", help="真正创建")

    p_add = sub.add_parser("add", help="追加一条备忘")
    p_add.add_argument("text", help="内容")
    p_add.add_argument("--apply", action="store_true", help="真正写入")

    p_show = sub.add_parser("show", help="读一条备忘的正文")
    p_show.add_argument("note_id")

    args = ap.parse_args()

    try:
        if args.cmd == "folders":
            for fid, name in list_folders():
                print(f"  {name}")
                print(f"      {fid}")
            return 0

        if args.cmd == "list":
            memos = snapshot()
            print(f"备忘文件夹：{active_folder_id()}")
            print(f"共 {len(memos)} 条：")
            for m in memos:
                print(f"  · {m.name}")
                print(f"      {m.note_id}")
            return 0

        if args.cmd == "init":
            if not args.apply:
                existing = [n for _, n in list_folders() if n == args.name]
                print(f"【干跑】将确保文件夹「{args.name}」存在"
                      f"（{'已存在，会复用' if existing else '不存在，会新建'}）")
                return 0
            fid, name, created = ensure_folder(args.name)
            save_folder_to_config(fid, name)
            print(f"{'✅ 已新建' if created else '✅ 已复用'}文件夹「{name}」")
            print(f"   id: {fid}")
            print(f"   已写入 config.json 的 memo_folder_id")
            return 0

        if args.cmd == "add":
            if not args.apply:
                print(f"【干跑】将追加备忘：{args.text!r}")
                return 0
            m = add(args.text)
            print(f"✅ 已追加：{m.display}")
            print(f"   note_id: {m.note_id}")
            return 0

        if args.cmd == "show":
            print(text_of(args.note_id))
            return 0

    except MemoError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
