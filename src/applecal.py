#!/usr/bin/env python3
"""
Apple 日历：只读快照 + 单向追加。

与 `memo.py` 同样的克制：**只有两个方向**，没有删除、没有修改。
删除与改时间都由你在日历里操作（见 docs/ARCHITECTURE.md 权限矩阵）。

## 关键技术决定：日期不用 AppleScript 字面量

AppleScript 里写 `date "2026年10月5日 下午2:00:00"` **依赖系统区域设置**，
换台机器、改个语言就可能解析失败或差一天。这类 bug 极难发现。

所以本模块的做法是：

    ① Python（whens.py）把时间算成 年/月/日/时/分 五个整数
    ② AppleScript 里 `set` 逐个字段构造日期对象

这样时间语义完全由 Python 负责（可彻底离线测试），
AppleScript 只做"赋值"这个机械动作，不受区域设置影响。

## 实测踩过的坑（沿用备忘录取得的结论）

  · 用 osascript 子进程而不是 PyObjC —— TCC 授权绑定调用进程身份，
    Terminal 已有稳定的自动化授权
  · 写入可能静默失败 → 写完必须读回验证
  · 事件的 summary 可能被规范化 → 用 uid 或起止时间定位，不用标题比对
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"

APP = "Calendar"

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


class CalendarError(Exception):
    """日历操作失败（带可操作的提示）。"""


# ── AppleScript 通道

def _as_literal(s: str) -> str:
    """转成 AppleScript 字符串字面量。反斜杠必须先转义。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_applescript(src: str, timeout: int = 45) -> str:
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # 日历冷启动比备忘录慢（它会加载整个数据库），超时给得更宽
        raise CalendarError("AppleScript 超时（日历可能在冷启动，稍后重试）") from None

    err = p.stderr.strip()
    if err:
        if "-10004" in err or "privilege violation" in err:
            raise CalendarError(
                "日历拒绝访问（-10004 越权）。请到 "
                "系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「日历」")
        if "-1743" in err:
            raise CalendarError("系统不允许向日历发送 Apple 事件（-1743），同上需授权")
        if "-1728" in err:
            raise CalendarError(f"对象不存在（-1728）：{err}")
        raise CalendarError(f"AppleScript 失败：{err}")
    return p.stdout.strip()


def config_calendar() -> str:
    """读 config.json 里的目标日历名。"""
    if not CONFIG.is_file():
        return ""
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return (cfg.get("calendar_name") or "").strip()


def create_calendar(name: str) -> tuple[str, bool]:
    """
    新建一个日历，返回 (名字, 是否新建)。

    同名已存在则直接复用（**不新建第二个**）—— 名字可重复，
    所以必须自己遍历比对，不能靠 AppleScript 的 whose。
    """
    existing = dict(list_calendars())
    if name in existing:
        return name, False

    run_applescript(
        f'tell application "{APP}"\n'
        f'  make new calendar with properties {{name:{_as_literal(name)}}}\n'
        '  return "ok"\n'
        'end tell')

    # 读回确认（防静默失败）
    if name in dict(list_calendars()):
        return name, True
    raise CalendarError(f"新建日历失败（读回时找不到 {name!r}）")


def save_calendar_to_config(name: str) -> None:
    cfg: dict = {}
    if CONFIG.is_file():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
    cfg["calendar_name"] = name
    cfg["calendar_updated_at"] = dt.datetime.now().astimezone().replace(
        microsecond=0).isoformat()
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                      encoding="utf-8")


# ── 数据模型

@dataclass
class Event:
    """日历里的一条事件。"""
    summary: str
    start: dt.datetime
    end: dt.datetime
    calendar: str = ""
    location: str = ""
    uid: str = ""

    @property
    def display(self) -> str:
        wd = "一二三四五六日"[self.start.isoweekday() - 1]
        return (f"{self.start.month}月{self.start.day}日 周{wd} "
                f"{self.start.hour:02d}:{self.start.minute:02d} {self.summary}")


# ── 只读快照

def list_calendars() -> list[tuple[str, bool]]:
    """列出所有日历 (名字, 是否可写)。只读。"""
    out = run_applescript(
        f'tell application "{APP}"\n'
        '  set out to ""\n'
        '  set FS to character id 1\n'
        '  set RS to character id 2\n'
        '  repeat with c in calendars\n'
        f'    set out to out & (name of c) & FS & (writable of c) & RS\n'
        '  end repeat\n'
        '  return out\n'
        'end tell')
    pairs: list[tuple[str, bool]] = []
    for chunk in out.split("\u0002"):
        chunk = chunk.strip("\n\r")
        if "\u0001" not in chunk:
            continue
        name, wr = chunk.split("\u0001", 1)
        pairs.append((name.strip(), wr.strip().lower() == "true"))
    return pairs


def writable_calendars() -> list[str]:
    return [n for n, w in list_calendars() if w]


def events_between(start: dt.date, end: dt.date,
                   calendar: str | None = None) -> list[Event]:
    """
    读某段日期内的事件（只读）。`end` 不含当天。

    ⚠️ **Calendar 没在运行时，这个读会失败**（2026-10-05 实测）：

        execution error: 「Calendar」發生錯誤：應用程式不在執行中。(-600)

    而**写**不受影响（`make new event` 会把 App 拉起来）—— 所以这个坑只在读路径上，
    它让日报的"明日日程"整整缺了两天（10-03 超时、10-04 -600），
    也让 `/list` 的日程段读不到。见 `_launch_calendar`。

    失败是**立刻**返回的（App 没跑，AppleScript 不做任何遍历），
    所以"拉起 + 重试一次"的代价只在真正需要时才付。
    """
    try:
        return _events_between_once(start, end, calendar)
    except CalendarError as e:
        if "-600" not in str(e):
            raise
        _launch_calendar()
        # 再失败就如实抛给调用方（它会写进回执/日报的告警段）——
        # 不吞、不重试第二次。
        return _events_between_once(start, end, calendar)


def _launch_calendar() -> None:
    """
    把 Calendar 拉起来（尽力而为，**绝不阻塞**）。

    为什么由代码来做：这是台 7×24 的机器，而"日历读不到"的解药只有
    "让 App 在跑" —— 让人每次手动去开 Calendar，等于这条链路上挂了个
    必须有人守着的开关（ARCHITECTURE 里最反对的那种）。

    ⚠️ 两种办法都试，是因为实测它们**在不同上下文里表现不同**
    （2026-10-05，借 launchd 身份跑了三次探针）：

      · `open -a Calendar` —— 在普通终端里立刻返回 0；
        在 launchd 的上下文里**卡住不返回**（探针的步骤标记停在那一行）。
        于是加 `-g`（后台开，不抢焦点）+ **短超时**：卡住就杀掉换下一个。
      · `osascript -e 'tell application "Calendar" to launch'` ——
        走 AppleScript 自己的 launch，守护对 Calendar 本来就有自动化授权。

    两个都失败就静默放弃：调用方会**如实抛错**，回执里写着"日历读不到"。
    **这一段的"能否真的拉起来"我无法在本机验证完整**（见 CHANGELOG），
    所以它写成了"失败也不影响如实报错"的形状。
    """
    for cmd in (["/usr/bin/open", "-g", "-a", APP],
                ["/usr/bin/osascript", "-e",
                 f'tell application "{APP}" to launch']):
        try:
            subprocess.run(cmd, capture_output=True, timeout=8)
            break
        except Exception:      # noqa: BLE001
            continue
    time.sleep(3)              # 给它起来的时间（实测这个量级够）


def _events_between_once(start: dt.date, end: dt.date,
                         calendar: str | None = None) -> list[Event]:
    """
    读某段日期内的事件（只读）。`end` 不含当天。

    用 `whose` 过滤会让 AppleScript 自己做日期比较（受区域设置影响），
    所以改为**把事件取出来、在 Python 里比较** —— 时间判断只在 Python 做，
    这是本模块的一贯原则。

    ⚠️ 代价是**遍历整个日历**：日历里事件多的时候这一步是秒级的
    （实测 /list 的日程段要十几秒）。要提速就得回到 `whose`，
    而那会把日期比较交给 AppleScript —— 那是本模块刻意避开的东西。
    暂时接受慢，见 docs/TELEGRAM-VOICE.md 里"宁可慢，不要区域设置 bug"。
    """
    cal = calendar or config_calendar()
    scope = (f'calendar {_as_literal(cal)}' if cal else "calendar 1")

    out = run_applescript(
        f'tell application "{APP}"\n'
        f'  set targetCal to {scope}\n'
        '  set out to ""\n'
        '  set FS to character id 1\n'
        '  set RS to character id 2\n'
        '  repeat with e in (every event of targetCal)\n'
        '    set sd to start date of e\n'
        '    set ed to end date of e\n'
        '    set out to out & (summary of e) & FS '
        '& (year of sd) & "-" & (month of sd as integer) & "-" & (day of sd) '
        '& " " & (hours of sd) & ":" & (minutes of sd) & FS '
        '& (year of ed) & "-" & (month of ed as integer) & "-" & (day of ed) '
        '& " " & (hours of ed) & ":" & (minutes of ed) & FS '
        '& (location of e) & FS & (uid of e) & RS\n'
        '  end repeat\n'
        '  return out\n'
        'end tell', timeout=120)

    return [e for e in parse_events(out, cal)
            if start <= e.start.date() < end]


# AppleScript 回传的"空值"字面量。
#
# ⚠️ 实测（2026-10-05）：日程**没填地点**时，`location of e` 回传的不是空串，
# 而是这个字面量 —— 于是回执 / 日报 / `/list` 里会出现 `@missing value`。
# 第一次真跑 `/list` 就把它打出来了（"全天 去龙井村　@missing value"）。
_AS_MISSING = "missing value"


def _as_text(s: str) -> str:
    """AppleScript 的文本 → Python 文本：`missing value` 归一成空串。"""
    s = (s or "").strip()
    return "" if s == _AS_MISSING else s


def parse_events(raw: str, calendar: str = "") -> list[Event]:
    """
    解析事件快照。抽成独立函数便于离线测试。

    格式（每条）：`summary\\u0001Y-M-D H:M\\u0001Y-M-D H:M\\u0001location\\u0001uid\\u0002`
    """
    events: list[Event] = []
    for chunk in raw.split("\u0002"):
        chunk = chunk.strip("\n\r")
        parts = chunk.split("\u0001")
        if len(parts) < 5:
            continue
        summary, s_str, e_str, location, uid = parts[0], parts[1], parts[2], parts[3], parts[4]
        start = _parse_dt(s_str)
        end = _parse_dt(e_str)
        if start is None or end is None:
            continue
        events.append(Event(summary=_as_text(summary), start=start, end=end,
                            calendar=calendar, location=_as_text(location),
                            uid=_as_text(uid)))
    return events


def _parse_dt(s: str) -> dt.datetime | None:
    """解析 AppleScript 回报的 `Y-M-D H:M`（注意月/日可能是 1 位）。"""
    s = s.strip()
    if not s:
        return None
    try:
        date_part, _, time_part = s.partition(" ")
        y, m, d = (int(x) for x in date_part.split("-"))
        hh, _, mm = time_part.partition(":")
        return dt.datetime(y, m, d, int(hh or 0), int(mm or 0))
    except (ValueError, TypeError):
        return None


def upcoming(days: int = 7, calendar: str | None = None) -> list[Event]:
    """未来 N 天的事件（含今天）。"""
    today = dt.date.today()
    evs = events_between(today, today + dt.timedelta(days=days), calendar)
    return sorted(evs, key=lambda e: e.start)


# ── 单向追加
#
# 日期构造：逐字段 set。这是本模块最重要的技术决定 ——
# 不用 `date "..."` 字面量（受系统区域设置影响，是"差一天"bug 的来源）。

def _set_date_script(var: str, when: dt.datetime) -> str:
    """生成逐字段构造日期的 AppleScript 片段。"""
    return (
        f'  set {var} to current date\n'
        f'  set year of {var} to {when.year}\n'
        f'  set month of {var} to {when.month}\n'
        f'  set day of {var} to {when.day}\n'
        f'  set hours of {var} to {when.hour}\n'
        f'  set minutes of {var} to {when.minute}\n'
        f'  set seconds of {var} to 0\n')


def add(summary: str, start: dt.datetime, end: dt.datetime | None = None,
        calendar: str | None = None, location: str = "",
        description: str = "", recurrence: str = "",
        allday: bool = False) -> Event:
    """
    新建一条日程，返回它（含 uid）。

    **写完读回验证**：日历的写入同样可能静默失败，
    所以建完后按 `uid` 查回确认存在 —— 不用标题比对（标题可能被规范化）。

    **精度到分钟**：秒被置为 0。输入是自然语言（"下午两点"），
    秒没有意义；而且固定为 0 能让"写入"与"读回"可精确比对
    （否则读回的秒数与写入时有偏差，看起来像 bug —— 实测踩到过：
    真实环境自检报"写入 15:40:06 / 读回 15:40:00"，其实是断言没对齐精度）。
    """
    summary = (summary or "").strip()
    if not summary:
        raise CalendarError("日程标题为空")

    end = end or (start + dt.timedelta(hours=1))
    if end <= start:
        raise CalendarError(f"结束时间不晚于开始时间：{start} → {end}")

    if allday:
        # ⚠️ **全天事件的时刻必须归零**（2026-10-05 修，用户实报）。
        #
        # `whens` 给全天事件的其实是"当天 09:00 + 24 小时"（它的默认时刻是 9 点，
        # `all_day` 只负责把时长凑成一天，没有把时刻归零）。而 Calendar 收到
        # `allday event:true` 后会把 **start 归零、end 却按原样留下** ——
        # 于是 `@明天去龙井村` 实际写成 10-05 00:00 → 10-06 09:00 的 **33 小时**
        # 事件，在日历上同时压在 5 日和 6 日两天。
        #
        # 为什么一直没被发现：两条读路径都按 `start` 的**日期**筛，
        # 所以它在我方看起来始终是"10-05 那一天"的一件事；
        # 而回执里写的是 `（全天）`（`whens.format_when` 看的是 all_day 标志）——
        # **只有打开日历用眼睛看才会发现**。
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)
        if end <= start:
            # 兜底：一天的全天事件 = 次日 00:00 结束（iCalendar 的排他写法）
            end = start + dt.timedelta(days=1)

    cal = calendar or config_calendar() or (writable_calendars() or [""])[0]
    if not cal:
        raise CalendarError("找不到可写的日历（config.json 里也没有 calendar_name）")

    props = [
        f'summary:{_as_literal(summary)}',
        'start date:startDate',
        'end date:endDate',
    ]
    if location:
        props.append(f'location:{_as_literal(location)}')
    if description:
        props.append(f'description:{_as_literal(description)}')
    if allday:
        props.append('allday event:true')
    if recurrence:
        props.append(f'recurrence:{_as_literal(recurrence)}')

    src = (
        f'tell application "{APP}"\n'
        f'  set targetCal to calendar {_as_literal(cal)}\n'
        + _set_date_script("startDate", start)
        + _set_date_script("endDate", end)
        + f'  set newEv to make new event at end of events of targetCal '
          f'with properties {{{", ".join(props)}}}\n'
        '  return uid of newEv\n'
        'end tell')

    uid = run_applescript(src, timeout=60)
    if not uid:
        raise CalendarError(f"新建日程未返回 uid（可能写入失败）：{summary!r}")

    return Event(summary=summary, start=start, end=end, calendar=cal,
                 location=location, uid=uid.strip())


def exists(uid: str, calendar: str | None = None) -> bool:
    """按 uid 确认日程存在（写入后的读回验证）。只读。"""
    if not uid:
        return False
    cal = calendar or config_calendar()
    scope = (f'calendar {_as_literal(cal)}' if cal else "calendar 1")
    out = run_applescript(
        f'tell application "{APP}"\n'
        f'  set targetCal to {scope}\n'
        '  repeat with e in (every event of targetCal)\n'
        f'    if (uid of e) is {_as_literal(uid)} then return "FOUND"\n'
        '  end repeat\n'
        '  return "MISSING"\n'
        'end tell', timeout=120)
    return out.strip() == "FOUND"


# ── CLI

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Apple 日历（只读快照 + 单向追加）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("calendars", help="列出所有日历（只读）")
    p_up = sub.add_parser("upcoming", help="未来若干天的日程（只读）")
    p_up.add_argument("--days", type=int, default=7)
    p_up.add_argument("--calendar")

    p_new = sub.add_parser("new", help="新建一个日历并设为目标")
    p_new.add_argument("name")
    p_new.add_argument("--apply", action="store_true")

    p_set = sub.add_parser("use", help="设置目标日历（写入 config.json）")
    p_set.add_argument("name")
    p_set.add_argument("--apply", action="store_true")

    p_add = sub.add_parser("add", help="新建一条日程")
    p_add.add_argument("summary")
    p_add.add_argument("--start", required=True, help="开始时间 YYYY-MM-DD HH:MM")
    p_add.add_argument("--end", help="结束时间 YYYY-MM-DD HH:MM")
    p_add.add_argument("--calendar")
    p_add.add_argument("--location", default="")
    p_add.add_argument("--recurrence", default="", help="iCal RRULE，如 FREQ=WEEKLY;BYDAY=MO")
    p_add.add_argument("--allday", action="store_true")
    p_add.add_argument("--apply", action="store_true")

    args = ap.parse_args()

    try:
        if args.cmd == "calendars":
            for name, wr in list_calendars():
                print(f"  {name}  [{'可写' if wr else '只读'}]")
            print()
            print(f"当前目标日历：{config_calendar() or '（未设置）'}")
            return 0

        if args.cmd == "upcoming":
            evs = upcoming(args.days, args.calendar)
            print(f"未来 {args.days} 天：{len(evs)} 项")
            for e in evs:
                loc = f"  @{e.location}" if e.location else ""
                print(f"  · {e.display}{loc}")
            return 0

        if args.cmd == "new":
            existing = dict(list_calendars())
            if args.name in existing:
                print(f"「{args.name}」已存在，将直接复用（不新建）")
            if not args.apply:
                print(f"【干跑】将新建日历「{args.name}」并设为写入目标")
                return 0
            _, created = create_calendar(args.name)
            save_calendar_to_config(args.name)
            print(f"{'✅ 已新建' if created else '✅ 已复用'}日历「{args.name}」")
            print(f"   已写入 config.json 的 calendar_name")
            return 0

        if args.cmd == "use":
            cals = dict(list_calendars())
            if args.name not in cals:
                print(f"❌ 没有名为「{args.name}」的日历。可用的：")
                for n, w in cals.items():
                    print(f"     {n}  [{'可写' if w else '只读'}]")
                return 1
            if not cals[args.name]:
                print(f"❌ 日历「{args.name}」是只读的，不能写入")
                return 1
            if not args.apply:
                print(f"【干跑】将把目标日历设为「{args.name}」")
                return 0
            save_calendar_to_config(args.name)
            print(f"✅ 目标日历已设为「{args.name}」")
            return 0

        if args.cmd == "add":
            start = dt.datetime.fromisoformat(args.start)
            end = dt.datetime.fromisoformat(args.end) if args.end else None
            if not args.apply:
                print(f"【干跑】将新建日程：{args.summary!r}")
                print(f"   {start} → {end or start + dt.timedelta(hours=1)}")
                if args.recurrence:
                    print(f"   重复：{args.recurrence}")
                return 0
            ev = add(args.summary, start, end, args.calendar, args.location,
                     recurrence=args.recurrence, allday=args.allday)
            ok = exists(ev.uid, args.calendar)
            print(f"{'✅ 已新建' if ok else '⚠️ 未读回'}：{ev.display}")
            print(f"   uid: {ev.uid}")
            return 0 if ok else 2

    except CalendarError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"❌ 时间格式错误（应为 YYYY-MM-DD HH:MM）：{e}", file=sys.stderr)
        return 1

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
