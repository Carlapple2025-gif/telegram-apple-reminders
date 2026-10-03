#!/usr/bin/env python3
"""
真实环境自检：验证三处 Apple 应用真的能读能写。

## 为什么需要

单元测试和端到端冒烟都用**假写入端**（不碰 Apple 应用），所以它们
证明不了"真的能写进去"。而"写入静默失败"是实测踩过的坑
（备忘录会不报错但没生效）。这个脚本做真实写入，每步都读回验证。

## 会创建什么、会不会留下垃圾

在**专用测试容器**里建东西，跑完自动清理：

    提醒事项 → 临时列表 PDCA-SELFTEST-<pid>
    备忘录   → 临时文件夹 PEMO-SELFTEST-<pid>
    日历     → 临时日历 CAL-SELFTEST-<pid>

**全部删干净**（这三处都是测试自己建的，不是你的数据）。
若中途失败，脚本会打印残留物与手动清理命令。

## 用法

    python3 tools/selftest-live.py            # 完整：读写 + 清理
    python3 tools/selftest-live.py --readonly # 只验证读权限
    python3 tools/selftest-live.py --keep     # 保留测试数据（排查用）
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PID = os.getpid()
REM_LIST = f"PDCA-SELFTEST-{PID}"
MEMO_FOLDER = f"PEMO-SELFTEST-{PID}"
CAL_NAME = f"CAL-SELFTEST-{PID}"

failures: list[str] = []
checks = 0


def check(label: str, cond: bool, extra: str = "") -> bool:
    global checks
    checks += 1
    if cond:
        print(f"  ✅ {label}")
    else:
        failures.append(f"{label} {extra}")
        print(f"  ❌ {label} {extra}")
    return cond


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 50 - len(title)))


def osa(src: str, timeout: int = 60) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时"
    if p.stderr.strip():
        return False, p.stderr.strip().splitlines()[0][:110]
    return True, p.stdout.strip()


def lit(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── 读权限（三处）

def test_read() -> None:
    section("读权限")
    for label, src in [
        ("备忘录", 'tell application "Notes"\n  return count of folders\nend tell'),
        ("日历", 'tell application "Calendar"\n  return count of calendars\nend tell'),
        ("提醒事项", 'tell application "Reminders"\n  return count of lists\nend tell'),
    ]:
        ok, out = osa(src)
        check(f"{label}可读", ok, out)


# ── 提醒事项：建列表 → 建条目 → 读回 → 置完成 → 清理

def test_reminders() -> None:
    section("提醒事项（建列表 / 建条目 / 置完成 / 删除）")
    import reminders as R

    # 建临时列表
    ok, out = osa('tell application "Reminders"\n'
                  f'  make new list with properties {{name:{lit(REM_LIST)}}}\n'
                  '  return "ok"\nend tell')
    if not check("建临时列表", ok, out):
        return

    try:
        # Reminders 接受 config 字典，列表名从 reminders_list 读
        rem = R.Reminders({"reminders_list": REM_LIST})
        n = rem.verify_list()
        check("列表可读（verify_list）", n == 0, f"条数={n}")

        text = f"自检条目 {PID}"
        r = rem.create(name=text, body=R.make_key(text))
        check("建条目返回对象", r is not None and bool(getattr(r, "id", "")))

        items = rem.all_reminders()
        check("读回能看见新条目", any(i.name == text for i in items),
              f"实际 {[i.name for i in items]}")

        target = next((i for i in items if i.name == text), None)
        if target is not None:
            rem.set_completed(target.id, True)
            again = rem.all_reminders()
            done = next((i for i in again if i.name == text), None)
            check("置完成后读回为已完成",
                  done is not None and bool(done.completed))
    finally:
        ok, out = osa('tell application "Reminders"\n'
                      f'  delete list {lit(REM_LIST)}\n'
                      '  return "ok"\nend tell')
        check("清理临时列表", ok, out)


# ── 备忘录：建文件夹 → 追加 → 快照读回 → 清理

def test_memo() -> None:
    section("备忘录（建文件夹 / 追加 / 快照 / 删除）")
    import memo as M

    ok, out = osa('tell application "Notes"\n'
                  f'  make new folder with properties {{name:{lit(MEMO_FOLDER)}}}\n'
                  '  return "ok"\nend tell')
    if not check("建临时文件夹", ok, out):
        return

    # 找到它的 id（备忘录必须按 id 定位）
    fid = None
    for f_id, f_name in M.list_folders():
        if f_name == MEMO_FOLDER:
            fid = f_id
            break
    if not check("能按名字找到临时文件夹 id", fid is not None):
        return

    try:
        # 临时把 memo 的当前文件夹指向测试文件夹
        _orig = M.active_folder_id
        M.active_folder_id = lambda: fid          # type: ignore[assignment]
        try:
            before = M.snapshot_ids()
            text = f"自检备忘 {PID}"
            m = M.add(text)
            check("追加返回 note_id", bool(m.note_id))
            after = M.snapshot_ids()
            check("快照能看见新笔记", m.note_id in after,
                  f"before={len(before)} after={len(after)}")
            check("读回正文正确", text in (M.text_of(m.note_id) or ""))
        finally:
            M.active_folder_id = _orig           # type: ignore[assignment]
    finally:
        ok, out = osa('tell application "Notes"\n'
                      f'  delete folder id {lit(fid)}\n'
                      '  return "ok"\nend tell')
        check("清理临时文件夹", ok, out)


# ── 日历：建日历 → 建事件（含重复规则）→ 读回 → 清理

def test_calendar() -> None:
    section("日历（建日历 / 建事件 / 重复规则 / 删除）")
    import applecal as A

    existing = dict(A.list_calendars())
    ok, out = osa('tell application "Calendar"\n'
                  f'  make new calendar with properties {{name:{lit(CAL_NAME)}}}\n'
                  '  return "ok"\nend tell')
    if not check("建临时日历", ok, out):
        return
    check("新日历可写", dict(A.list_calendars()).get(CAL_NAME) is True)

    try:
        start = dt.datetime.now().replace(microsecond=0) + dt.timedelta(days=1)
        end = start + dt.timedelta(hours=1)
        summary = f"自检日程 {PID}"

        ev = A.add(summary, start, end, calendar=CAL_NAME, location="自检地点")
        check("建事件返回 uid", bool(ev.uid))

        # 读回：只查明天
        day = start.date()
        evs = A.events_between(day, day + dt.timedelta(days=1), calendar=CAL_NAME)
        found = [e for e in evs if e.summary == summary]
        check("读回能看见新事件", bool(found), f"读到 {[e.summary for e in evs]}")
        if found:
            e = found[0]
            check("时间逐字段构造正确（不差一天/不差时区）",
                  e.start == start, f"写入 {start} / 读回 {e.start}")
            check("地点写入正确", e.location == "自检地点", f"得到 {e.location!r}")

        # 重复规则（日历独有的能力）
        ev2 = A.add(f"自检重复 {PID}", start, end, calendar=CAL_NAME,
                    recurrence="FREQ=WEEKLY;BYDAY=MO")
        check("带重复规则的事件可建", bool(ev2.uid))

        ok, out = osa('tell application "Calendar"\n'
                      f'  set c to calendar {lit(CAL_NAME)}\n'
                      '  repeat with e in (every event of c)\n'
                      '    if (recurrence of e) is not missing value then return "HAS"\n'
                      '  end repeat\n'
                      '  return "NONE"\nend tell')
        check("重复规则真的写进去了", ok and out == "HAS", f"{out}")
    finally:
        ok, out = osa('tell application "Calendar"\n'
                      f'  delete calendar {lit(CAL_NAME)}\n'
                      '  return "ok"\nend tell')
        check("清理临时日历", ok, out)


def leftover_note() -> None:
    print()
    print("若上面有清理失败，手动删除这几项即可（都是自检建的）：")
    print(f"  提醒事项列表：{REM_LIST}")
    print(f"  备忘录文件夹：{MEMO_FOLDER}")
    print(f"  日历：{CAL_NAME}")


def main() -> int:
    readonly = "--readonly" in sys.argv
    keep = "--keep" in sys.argv

    print("═" * 58)
    print("真实环境自检" + ("（只读）" if readonly else "（会真实读写，跑完清理）"))
    print("═" * 58)

    test_read()
    if readonly:
        print()
        print("═" * 58)
        print(f"{'✅ 读权限全部可用' if not failures else f'❌ 失败 {len(failures)} 项'}"
              f"（{checks} 项）")
        print("═" * 58)
        return 1 if failures else 0

    test_reminders()
    test_memo()
    test_calendar()

    print()
    print("═" * 58)
    if failures:
        print(f"❌ 失败 {len(failures)}/{checks} 项：")
        for f in failures:
            print(f"   · {f}")
        if not keep:
            leftover_note()
    else:
        print(f"✅ 三处真实读写全部通过（{checks} 项）")
        print("   （测试数据已清理，未留下任何东西）")
    print("═" * 58)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
