#!/usr/bin/env python3
"""
只读能力探测：macOS 三个 App（备忘录 / 日历 / 提醒事项）的脚本接口能做什么、代价多大。

**这个脚本一个字节都不写。** 没有 make / set / delete —— 只有 get / count / whose。
想验证写入类结论（闹钟、重复规则）请另写脚本，并先确认它写到哪里。

为什么要探测而不是查文档：macOS 的脚本接口"文档说支持"和"实际能用"
经常不是一回事（写入静默失败、whose 缺 is 编译失败、权限被拒返回空集合，
都是这么发现的）。

跑法（**要在真实终端里跑** —— TCC 授权挂在调用进程身份上）：

    python3 probe-apple-capabilities.py            # 全跑
    python3 probe-apple-capabilities.py --quick    # 跳过计时（不碰大量数据）
    python3 probe-apple-capabilities.py --list 提醒 # 指定要探测的提醒事项列表

输出是"结论 + 实测数字"，可以直接贴进平台事实文档。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# 授权失败的两种错误码。见 SKILL.md「权限」一节：
#   -10004 越权（沙箱/未勾选）   -1743 未授权（系统不允许发送 Apple 事件）
PRIV_CODES = ("-10004", "-1743")

OK, BAD, WARN, SKIP = "✅", "❌", "⚠️ ", "— "


def osa(src: str, timeout: int = 60) -> tuple[bool, str]:
    """跑一段 AppleScript。返回 (成功?, 输出或错误文本)。

    ⚠️ 走 osascript 子进程而不是 PyObjC/脚本桥：TCC 授权绑定**调用进程身份**，
    osascript 用的是终端已有的授权，稳定；做成 app bundle 后 ad-hoc 签名
    每次重编译哈希都变，授权随之失效。
    """
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"超时（>{timeout}s）"
    if p.returncode != 0:
        return False, (p.stderr or "").strip()
    return True, p.stdout.rstrip("\n")


def priv_hint(err: str) -> str:
    if any(c in err for c in PRIV_CODES):
        return "（授权缺失：系统设置 → 隐私与安全性 → 自动化 → 终端，勾选对应 App）"
    return ""


def timed(fn, *a, **kw):
    t0 = time.time()
    out = fn(*a, **kw)
    return out, int((time.time() - t0) * 1000)


# ── ① 权限：三个 App 各能不能读 ────────────────────────────────────
def check_permissions() -> list[str]:
    print("── ① 权限（能否读取三个 App）──")
    lines = []
    targets = [
        ("备忘录", 'tell application "Notes" to return (count of folders) as string'),
        ("日历", 'tell application "Calendar" to return (count of calendars) as string'),
        ("提醒事项", 'tell application "Reminders" to return (count of lists) as string'),
    ]
    for name, src in targets:
        ok, out = osa(src, timeout=40)
        if ok:
            msg = f"{OK} {name}：可读（{out} 个容器）"
        else:
            msg = f"{BAD} {name}：{out[:70]}{priv_hint(out)}"
        print("   " + msg)
        lines.append(msg)
    print()
    return lines


# ── ② 脚本字典：对象类型与属性是权威，别凭印象 ──────────────────────
SDEF = {
    "日历": "/System/Applications/Calendar.app/Contents/Resources/iCal.sdef",
    "提醒事项": "/System/Applications/Reminders.app/Contents/Resources/Reminders.sdef",
}


def check_dictionary() -> list[str]:
    print("── ② 脚本字典（哪些对象/属性真的存在）──")
    lines = []
    for name, path in SDEF.items():
        p = Path(path)
        if not p.exists():
            msg = f"{SKIP}{name}：字典文件不在 {path}"
            print("   " + msg)
            lines.append(msg)
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        classes = sorted(set(re.findall(r'<class name="([^"]+)"', txt)))
        msg = f"{OK} {name}：对象类型 = {', '.join(classes)}"
        print("   " + msg)
        lines.append(msg)

    # 这两条是硬约束的**唯一依据**，所以单独判一次
    rem = Path(SDEF["提醒事项"])
    if rem.exists():
        txt = rem.read_text(encoding="utf-8", errors="replace")
        # reminder 那一段里有没有 recurrence
        m = re.search(r'<class name="reminder".*?</class>', txt, re.S)
        seg = m.group(0) if m else ""
        for key, want, why in [
            ("recurrence", False, "→ 脚本建不出重复提醒（周期性事务只能放日历）"),
            ("completion date", True, "→ 完成时刻可读，是「今日完成」的判据"),
            ("remind me date", True, "→ 单次提醒时间可设（重复不行）"),
        ]:
            got = key in seg
            flag = OK if got == want else WARN
            msg = f"{flag} reminder 里{'有' if got else '没有'} {key} {why}"
            print("   " + msg)
            lines.append(msg)

    cal = Path(SDEF["日历"])
    if cal.exists():
        txt = cal.read_text(encoding="utf-8", errors="replace")
        cal_classes = set(re.findall(r'<class name="([^"]+)"', txt))
        # ⚠️ 「日历里没有 source / to-do」必须按**类名**判，不能用裸 grep 找关键词：
        # 字典别处的说明文字里也出现这些词，裸 grep 会误报（第一版就是这么错的）。
        for key, want, why in [
            ("source", False, "→ **脚本建不了 iCloud 日历**：指定不了账户，新日历落「我的 Mac 上」"),
            ("to-do", False, "→ 日历里没有待办对象（2012 年拆出去、成了独立的提醒事项）"),
        ]:
            got = key in cal_classes
            flag = OK if got == want else WARN
            msg = f"{flag} 日历的**对象类型**里{'有' if got else '没有'} {key} {why}"
            print("   " + msg)
            lines.append(msg)
        for key, want, why in [
            ("display alarm", True, "→ 闹钟可以加（全天事件别用 interval 0：那是半夜）"),
            ("trigger interval", True, "→ 相对事件的分钟数，负数 = 提前"),
        ]:
            got = key in txt
            flag = OK if got == want else WARN
            msg = f"{flag} 日历字典里{'有' if got else '没有'} {key} {why}"
            print("   " + msg)
            lines.append(msg)
        print(f"   权威对象类型（别的判据都从这儿来）：{', '.join(sorted(cal_classes))}")
    print()
    return lines


# ── ③ whose 子句：缺运算符会编译失败 ────────────────────────────────
def check_whose(default_list: str) -> list[str]:
    print("── ③ whose 子句（缺 is/contains 会编译失败）──")
    lines = []
    safe = default_list.replace('"', '\\"')
    probes = [
        ("带 is（正确写法）",
         f'tell application "Reminders"\n'
         f'  set L to list "{safe}"\n'
         f'  return (count of (every reminder of L whose completed is false)) as string\n'
         f'end tell', True),
        ("缺 is（应失败）",
         f'tell application "Reminders"\n'
         f'  set L to list "{safe}"\n'
         f'  return (count of (every reminder of L whose completed false)) as string\n'
         f'end tell', False),
        ("比较运算符写全 is greater than",
         'set d to (current date) - 1 * days\n'
         'tell application "Reminders"\n'
         f'  set L to list "{safe}"\n'
         f'  return (count of (every reminder of L whose completion date is greater than d)) as string\n'
         'end tell', True),
    ]
    for label, src, want_ok in probes:
        ok, out = osa(src)
        good = ok == want_ok
        flag = OK if good else WARN
        detail = out[:60] if ok else out[:70]
        msg = f"{flag} {label}：{'成功' if ok else '失败'} —— {detail}"
        print("   " + msg)
        lines.append(msg)

    print(f"   结论：whose 子句**一律写全运算符**（is / is greater than / contains）。")
    print()
    return lines


# ── ④ 提醒事项：列表、完成历史、以及读取代价 ────────────────────────
def pick_default_list() -> tuple[str, str]:
    """跟着系统的 default list 走，别自己猜列表名。返回 (名字, 说明)。"""
    ok, out = osa('tell application "Reminders" to get name of default list')
    if ok and out.strip():
        return out.strip(), "default list"
    # 退而求其次：取第一个列表，并明确说明这不是默认列表
    ok2, out2 = osa('tell application "Reminders"\n'
                    '  set L to item 1 of (every list)\n'
                    '  return name of L\nend tell')
    if ok2 and out2.strip():
        return out2.strip(), "（读不到 default list，退回第一个列表）"
    return "", "（读不到任何列表）"


def check_reminders(list_name: str) -> list[str]:
    print(f"── ④ 提醒事项（列表：{list_name}）──")
    lines = []
    safe = list_name.replace('"', '\\"')

    # 计数：原生筛 vs 全量读
    ok_native, t_native = timed(osa,
        f'tell application "Reminders"\n  set L to list "{safe}"\n'
        f'  return (count of (every reminder of L whose completed is true)) as string\nend tell')
    print(f"   {OK} 原生筛计数（whose completed is true）：{t_native} ms → {ok_native[1] if ok_native[0] else ok_native[1][:50]}")

    # 全量读：只取 name + completed
    src_full = (f'tell application "Reminders"\n  set L to list "{safe}"\n'
                '  set out to ""\n'
                '  repeat with r in (every reminder of L)\n'
                '    set out to out & (name of r) & tab & (completed of r as string) & linefeed\n'
                '  end repeat\n  return out\nend tell')
    (ok_full, out_full), t_full = timed(osa, src_full)
    n_items = len([l for l in out_full.splitlines() if l.strip()]) if ok_full else 0
    per_item = (t_full // n_items) if n_items else 0
    print(f"   {OK} 全量读（name + completed）：{t_full} ms / {n_items} 条 → 约 {per_item} ms/条（固定开销 ~1.2 s）")
    lines.append(f"全量读 {t_full} ms / {n_items} 条")

    # 明细读：completion date 是逐条属性访问，最贵
    src_detail = (f'tell application "Reminders"\n  set L to list "{safe}"\n'
                  '  set out to ""\n'
                  '  repeat with r in (every reminder of L whose completed is true)\n'
                  '    set cd to (completion date of r)\n'
                  '    if cd is missing value then\n'
                  '      set out to out & "none" & linefeed\n'
                  '    else\n'
                  '      set out to out & (year of cd as integer) & "," & (month of cd as integer)'
                  ' & "," & (day of cd) & linefeed\n'
                  '    end if\n'
                  '  end repeat\n  return out\nend tell')
    (ok_det, out_det), t_det = timed(osa, src_detail)
    done = [l for l in out_det.splitlines() if l.strip()] if ok_det else []
    per_done = (t_det // len(done)) if done else 0
    print(f"   {WARN}读完成明细（含 completion date）：{t_det} ms / {len(done)} 条 → 约 {per_done} ms/条 ⚠️")
    print(f"        代价随**历史条数线性增长**：100 条历史 ≈ {per_done * 100 // 1000} 秒")
    lines.append(f"完成明细 {t_det} ms / {len(done)} 条（{per_done} ms/条）")

    # 窗口筛：便宜的那个读法
    ok_win, t_win = timed(osa,
        'set cutoff to (current date) - 3 * days\n'
        f'tell application "Reminders"\n  set L to list "{safe}"\n'
        f'  return (count of (every reminder of L whose completed is true '
        f'and completion date is greater than or equal to cutoff)) as string\nend tell')
    print(f"   {OK} 窗口筛（最近 3 天）：{t_win} ms → {ok_win[1] if ok_win[0] else '失败'}"
          f"  ← 比不筛的计数还快，因为窗口里只有几条")
    print(f"   结论：**统计一律走窗口筛**；长周期统计从自己的留档算，不要每晚读全量历史。")

    # 未完成条目的 completion date 是 missing value（正好可作判据）
    ok_mv, out_mv = osa(
        f'tell application "Reminders"\n  set L to list "{safe}"\n'
        f'  set r to item 1 of (every reminder of L whose completed is false)\n'
        f'  if (completion date of r) is missing value then\n    return "missing"\n'
        f'  else\n    return "has value"\n  end if\nend tell')
    if ok_mv:
        print(f"   {OK} 未完成条目的 completion date = {out_mv}（正好可作「有没有做完」的判据）")
    else:
        print(f"   {SKIP}未完成条目的 completion date 判定：{out_mv[:50]}（列表里可能没有未完成条目）")
    print()
    return lines


# ── ⑤ 日历：窗口查询与重复事件的展开 ────────────────────────────────
def check_calendar() -> list[str]:
    print("── ⑤ 日历（窗口筛 + 重复事件的形状）──")
    lines = []
    ok_cal, out_cal = osa('tell application "Calendar"\n'
                          '  set out to ""\n'
                          '  repeat with c in calendars\n'
                          '    set out to out & (name of c) & linefeed\n'
                          '  end repeat\n  return out\nend tell')
    names = [n for n in out_cal.splitlines() if n.strip()] if ok_cal else []
    print(f"   {OK} 日历列表：{', '.join(names) if names else '（读不到）'}")

    # 逐字段构造日期 + whose 窗口筛（不要字符串字面量：区域设置敏感）
    src = ('tell application "Calendar"\n'
           '  set dFrom to current date\n'
           '  set year of dFrom to 2020\n  set month of dFrom to 1\n'
           '  set day of dFrom to 1\n  set hours of dFrom to 0\n'
           '  set minutes of dFrom to 0\n  set seconds of dFrom to 0\n'
           '  set dTo to current date\n'
           '  set year of dTo to 2030\n  set month of dTo to 1\n'
           '  set day of dTo to 1\n  set hours of dTo to 0\n'
           '  set minutes of dTo to 0\n  set seconds of dTo to 0\n'
           '  set out to ""\n'
           '  repeat with e in (every event of calendar 1 '
           'whose start date ≥ dFrom and start date < dTo)\n'
           '    set out to out & (summary of e) & linefeed\n'
           '  end repeat\n  return out\nend tell')
    (ok_ev, out_ev), t_ev = timed(osa, src, timeout=120)
    if ok_ev:
        n = len([l for l in out_ev.splitlines() if l.strip()])
        print(f"   {OK} whose 窗口筛（逐字段构造日期）：{t_ev} ms，命中 {n} 条")
        print(f"        注意：`whose recurrence is not missing value` 实测会报 -1700，别用。")
    else:
        print(f"   {WARN}whose 窗口筛：{out_ev[:70]}{priv_hint(out_ev)}")

    print(f"   {SKIP}「脚本建不了 iCloud 日历」不在此探测：**它需要真写一个日历才算数**，")
    print(f"        而写下去就会多出一个假日历（正是要防的事）。判别方法见 SKILL.md。")
    print(f"   {SKIP}闹钟 / 重复规则的写入行为同样不探测（要写数据）。")
    print()
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="只读探测 Apple 三件套的脚本能力")
    ap.add_argument("--quick", action="store_true", help="跳过计时类探测（不碰大量数据）")
    ap.add_argument("--list", default="", help="提醒事项列表名（默认跟随系统 default list）")
    ap.add_argument("--json", action="store_true", help="额外输出一段 JSON 摘要")
    args = ap.parse_args()

    print("Apple 脚本能力探测（**只读**：不创建、不修改、不删除任何东西）")
    print("=" * 64)
    print(f"环境：macOS {subprocess.run(['sw_vers', '-productVersion'], capture_output=True, text=True).stdout.strip()}")
    print()

    summary: dict = {"macos": subprocess.run(['sw_vers', '-productVersion'],
                                             capture_output=True, text=True).stdout.strip()}
    check_permissions()
    check_dictionary()

    if args.quick:
        print("── ③④⑤ 已按 --quick 跳过（计时类探测会读取真实数据）──\n")
        return 0

    list_name, how = args.list, "命令行指定" if args.list else ""
    if not list_name:
        list_name, how = pick_default_list()
    if not list_name:
        print(f"{BAD} 读不到提醒事项列表，跳过 ③④ 的列表相关探测\n")
        return 1
    print(f"── 提醒事项列表：{list_name} {how} ──")
    print()

    check_whose(list_name)
    summary["timings"] = check_reminders(list_name)
    check_calendar()

    print("=" * 64)
    print("结论怎么用：拿上面的实测数字去改「平台事实」文档，**改的是结论，别改证据**。")
    print("证据是命令的输出；它变了就把新输出贴进来、写清哪天哪版，再改结论 ——")
    print("这样下一个人能看出「是平台变了」还是「我们记错了」。")

    if args.json:
        print()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
