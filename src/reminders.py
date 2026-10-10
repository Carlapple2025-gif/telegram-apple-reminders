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
  3. **所有操作限定在一个列表里**（配置指定，或**系统的默认列表**），
     不碰你其它列表
  4. **写入后连字段一起读回**（旗标 / 优先级 / 到期日都要确认）——
     这里失效是**静默的**：你看不到旗标、进不了"已编排"，
     只会以为"我明明设了"

⚠️ **列表名**（2026-10-07 起）：`config.json` 的 `reminders_list` 若为**空**，
就跟随提醒事项里的**默认列表**（字典里 `default list` 是只读属性，读得到）。
原先固定写 `PDCA` —— 那是 v1 遗留的列表，已弃用。
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RemindersError(RuntimeError):
    pass


def _parse_completion(raw: str) -> dt.datetime | None:
    """
    把 AppleScript 传来的完成时刻（"年,月,日,时,分"）转成 datetime。

    `none` 与任何解析不出来的值都返回 None —— 调用方据此判断
    "这条没有完成时刻"（未完成的条目就是 missing value → none）。
    解析失败**不抛异常**：日报是只读汇总，一条读不出来不该让整份报告失败。
    """
    s = (raw or "").strip()
    if not s or s.lower() == "none":
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 5:
        return None
    try:
        y, mo, d, h, mi = (int(p) for p in parts[:5])
        return dt.datetime(y, mo, d, h, mi)
    except (ValueError, TypeError):
        return None


def _parse_due(raw: str) -> dt.datetime | None:
    """
    解析读回的到期日。AppleScript 那边按"年,月,日[,时,分]"回传，
    解析不出来返回 None（调用方据此判定"没写进去"）。
    """
    s = (raw or "").strip()
    if not s or s.lower() == "none":
        return None
    parts = [p.strip() for p in s.split(",")]
    try:
        y, mo, d = (int(p) for p in parts[:3])
        h = int(parts[3]) if len(parts) > 3 else 0
        mi = int(parts[4]) if len(parts) > 4 else 0
        return dt.datetime(y, mo, d, h, mi)
    except (ValueError, TypeError, IndexError):
        return None


def lit(s: str) -> str:
    """转成 AppleScript 字符串字面量。反斜杠必须最先转义。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _date_script(var: str, when: dt.datetime) -> str:
    """
    生成"逐字段构造日期"的 AppleScript 片段。

    ⚠️ **不用日期字面量**（`date "2026年10月7日 下午2:00:00"`）：那种写法
    要先解析字符串，而解析受系统区域设置影响 —— 换台机器/改个语言就可能差一天，
    且这类 bug 极难发现（`applecal.py` 顶部有完整说明，这里是同一条规矩）。
    逐字段赋值全程不经过任何字符串解析。
    """
    return (
        f'  set {var} to current date\n'
        f'  set year of {var} to {when.year}\n'
        f'  set month of {var} to {when.month}\n'
        f'  set day of {var} to {when.day}\n'
        f'  set hours of {var} to {when.hour}\n'
        f'  set minutes of {var} to {when.minute}\n'
        f'  set seconds of {var} to 0\n')


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
    # 完成时刻。**探测已证实可读**（tools/probe-native-dates.py）：
    # 已完成条目读得到真实日期，未完成的是 missing value。
    # 有了它，日报才能只报"今天完成的"，而不是把历史全倒出来。
    completed_at: dt.datetime | None = None


class Reminders:
    """
    限定在某个列表内的提醒事项访问。

    列表名来自 `config.json` 的 `reminders_list`；**为空时跟随系统的默认列表**
    （读字典里的 `default list`，只读属性）。列表不存在时会**明确报错**，
    而不是默默用别的列表 —— 写错列表会让待办混进用户真实数据。
    """

    def __init__(self, config: dict | None = None):
        if config is None:
            p = ROOT / "config.json"
            if not p.is_file():
                raise RemindersError(
                    "缺少 " + str(p) + "\n"
                    "请先运行：bash deploy/setup-v4.sh --apply")
            config = json.loads(p.read_text(encoding="utf-8"))
        self.config = config
        # 列表名的解析**推迟到第一次用到**（见 list_name）：
        # 空 = 跟随系统的默认列表（2026-10-07 起；PDCA 是 v1 遗留，已弃用）。
        self._list_cfg = (config.get("reminders_list") or "").strip()
        self._list_resolved: str | None = None

    @property
    def list_name(self) -> str:
        """
        要操作的列表名。

        配置里写了就用它；**空的就跟随提醒事项的默认列表** ——
        这样"换默认列表"只用在提醒事项设置里改一次，
        项目不用跟着改配置（也就不会过期）。
        """
        if self._list_cfg:
            return self._list_cfg
        if self._list_resolved is None:
            name = run('tell application "Reminders" to get name of default list')
            name = (name or "").strip()
            if not name:
                raise RemindersError(
                    "提醒事项没有默认列表（default list 读回空）——"
                    "请在提醒事项里指定一个默认列表")
            self._list_resolved = name
        return self._list_resolved

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
                f"可在提醒事项里手动建一个同名列表，或运行："
                f"  bash deploy/setup-v4.sh --apply"
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

        输出格式：每条若干行（id / completed / name / body / 完成时刻），
        用「一行一个字段」而不是分隔符拼接 —— AppleScript 字面量里
        不能可靠嵌入控制字符（notes.py 踩过这个坑）。

        ⚠️ 完成时刻用**年月日时分整数**输出，不输出日期字符串。
        日期字符串形如"2026年10月3日 星期六 下午2:13:30"——
        它依赖系统区域设置，换台机器/改个语言就可能解析失败或差一天，
        而这类 bug 极难发现（备忘录那边已经有同源教训，见 applecal.py
        顶部关于"日期不用 AppleScript 字面量"的说明）。
        整数分量则由 Python 组装，时间语义完全可控。
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
            '    set cd to (completion date of r)\n'
            '    if cd is missing value then\n'
            '      set out to out & "none" & linefeed\n'
            '    else\n'
            '      set out to out & (year of cd as integer) & "," & '
            '(month of cd as integer) & "," & (day of cd) & "," & '
            '(hours of cd) & "," & (minutes of cd) & linefeed\n'
            '    end if\n'
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
                        completed_at=_parse_completion(
                            chunk[4] if len(chunk) > 4 else "none"),
                    ))
                chunk = []
            else:
                chunk.append(line)
        return items

    def completed_on(self, day: dt.date) -> list[Reminder]:
        """
        只取**在某一天完成**的条目（Apple 原生 `completion date`）。

        探测已证实（tools/probe-native-dates.py）：
          · 已完成条目的 completion date 读得到
          · `whose completion date is greater than <某时刻>` 服务端可过滤
          · 未完成条目的该字段是 missing value

        这里仍走 `all_reminders()` 再在 Python 里筛，不直接用 whose：
        这个列表的条目量很小，而 whose 的日期比较要把 AppleScript 日期
        对象拼进查询串（区域设置敏感的写法），收益不抵风险。
        真有性能问题再换成 whose —— 那时也已经有探针证明它可用。
        """
        return [r for r in self.all_reminders()
                if r.completed and r.completed_at is not None
                and r.completed_at.date() == day]

    def completed_since(self, days: int = 3) -> list[Reminder]:
        """
        **从 N 天前到现在**完成的条目（Apple 服务端 `whose` 过滤）。

        与 `completed_on()` 的差别是**代价**，这条是实测出来的：

        | 读法 | 7 条列表上的实测 |
        |---|---|
        | `completed_on()` → `all_reminders()` | 每条 4 次属性读取，~1 s/条 |
        | **`completed_since()`** → 原生 `whose` 先筛 | 窗口筛 **595 ms**（命中 2 条）|

        攒了 100 条历史时，前者就是 100 秒 —— 足够拖垮 21:30 的日报。
        所以**留档 / 统计一律用这条**；`completed_on()` 留给"确实全都要"的场合。

        ⚠️ 写全 `is greater than or equal to`，不简写 `>=` —— 见
        `yesterday_done_count()` 的说明（缺运算符的 `whose` 会编译失败）。
        实测依据：tools/probe-reminders-history.py（2026-10-10）。
        """
        out = run(
            f'set cutoff to (current date) - {int(days)} * days\n'
            'tell application "Reminders"\n'
            f'  set L to list {lit(self.list_name)}\n'
            '  set out to ""\n'
            '  repeat with r in (every reminder of L whose completed is true '
            'and completion date is greater than or equal to cutoff)\n'
            '    set out to out & (id of r) & linefeed\n'
            '    set out to out & (name of r) & linefeed\n'
            '    set cd to (completion date of r)\n'
            '    if cd is missing value then\n'
            '      set out to out & "none" & linefeed\n'
            '    else\n'
            '      set out to out & (year of cd as integer) & "," & '
            '(month of cd as integer) & "," & (day of cd) & "," & '
            '(hours of cd) & "," & (minutes of cd) & linefeed\n'
            '    end if\n'
            '    set out to out & "----" & linefeed\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        )
        items: list[Reminder] = []
        chunk: list[str] = []
        for line in out.splitlines():
            if line.strip() == "----":
                # 名字里可能带换行 → 中间几行都算名字（比 all_reminders 的
                # "取 chunk[2]" 更稳；id 固定首行、完成时刻固定末行）
                if len(chunk) >= 3:
                    items.append(Reminder(
                        id=chunk[0],
                        name="\n".join(chunk[1:-1]),
                        completed=True,
                        body="",
                        due="",
                        completed_at=_parse_completion(chunk[-1]),
                    ))
                chunk = []
            else:
                chunk.append(line)
        return items

    def open_reminders(self) -> list[Reminder]:
        return [r for r in self.all_reminders() if not r.completed]

    # ── 写（全部带读回验证）

    def yesterday_done_count(self) -> int:
        """
        昨天完成了几条（Apple **服务端**过滤，用原生 `whose`）。

        这条存在的意义不只是功能 —— 它是"原生 `whose` 真能用"的活证据：
        探测（tools/probe-native-dates.py）证实
        `whose completion date is greater than <某时刻>` 可用，
        未完成条目的该字段是 missing value。

        ⚠️ 必须写 `is greater than`，不能简写成 `>`：
        本项目的静态契约检查要求每个 whose 子句都带 is/contains，
        因为**缺 is 的写法实测会编译失败**。探测里 `>` 能过是因为
        那里是另一个上下文 —— 不值得为省四个单词去踩自己定的规矩。
        """
        out = run(
            'set d to (current date) - 1 * days\n'
            'tell application "Reminders"\n'
            f'  set L to list {lit(self.list_name)}\n'
            '  return (count of (every reminder of L whose completion date '
            'is greater than d)) as string\n'
            'end tell'
        )
        try:
            return int(out.strip())
        except ValueError:
            return 0

    def create(self, name: str, body: str = "",
               due: dt.datetime | None = None, allday_due: bool = False,
               remind: bool = False, flagged: bool = False,
               priority: int = 0) -> Reminder:
        """
        新建条目。**到期日是绝对时刻**（`due`），逐字段构造，不走日期字面量。

        | 参数组合 | 落成什么 | 到点会弹吗 |
        |---|---|---|
        | `due=…` | `due date` | 否 |
        | `due=…, remind=True` | `due date` + `remind me date` | ✅ 弹 |
        | `due=…, allday_due=True` | `allday due date`（只有日期）| 否（没有时刻）|
        | `due=None` | 不写任何日期 | 否 |

        `flagged` / `priority` 是提醒事项**原生**的组织方式（旗标进"已加上旗标"；
        优先级 0 无 / 1 高 / 5 中 / 9 低）。要它们，是因为用户按提醒事项**自己的
        智能列表**（今天 / 已编排 / 已加上旗标）管待办 —— 那些列表显示的正是这些字段。

        读回验证分两层（2026-10-07 加第二层）：
          ① id 差集 —— 不按名字（按名字可能撞车，notes.py 的教训）
          ② **按 id 读回我们写的那几个字段** —— AppleScript 会"不报错但没生效"，
             而这里失效是**静默的**：你看不到旗标、进不了"已编排"，
             只会以为"我明明设了"。
        """
        before = {r.id for r in self.all_reminders()}

        date_lines = ""
        props = [f'name:{lit(name)}', f'body:{lit(body or "")}']
        if due is not None:
            d = due.replace(second=0, microsecond=0)
            if allday_due:
                date_lines += _date_script("dueDate", d.replace(hour=0, minute=0))
                props.append("allday due date:dueDate")
            else:
                date_lines += _date_script("dueDate", d)
                props.append("due date:dueDate")
                if remind:
                    props.append("remind me date:dueDate")
        if flagged:
            props.append("flagged:true")
        if priority:
            props.append(f"priority:{int(priority)}")

        run(
            'tell application "Reminders"\n'
            f'  set L to list {lit(self.list_name)}\n'
            + date_lines
            + f'  make new reminder at L with properties {{{", ".join(props)}}}\n'
            '  return "ok"\n'
            'end tell'
        )

        fresh = [r for r in self.all_reminders() if r.id not in before]
        if not fresh:
            raise RemindersError(
                f"新建「{name}」后 id 集合没有新增 —— 写入未生效。"
            )
        new = fresh[0]
        self._verify_written(new.id, due=due, allday_due=allday_due,
                             flagged=flagged, priority=priority)
        return new

    def _verify_written(self, reminder_id: str, *, due: dt.datetime | None,
                        allday_due: bool, flagged: bool,
                        priority: int) -> None:
        """
        按 id 读回刚写的那条，确认**我们要求的字段真的生效了**。

        只查"我们写过的东西" —— 这条规矩让读回保持廉价（一次 AppleScript、
        只读 4 个属性），同时把"静默失效"挡在写入那一刻，而不是等用户
        第二天发现"旗标怎么没打上"。
        """
        out = run(
            'tell application "Reminders"\n'
            f'  set hits to (every reminder whose id is {lit(reminder_id)})\n'
            '  if (count of hits) is 0 then return "NOTFOUND"\n'
            '  set r to item 1 of hits\n'
            '  set out to (flagged of r as string) & linefeed\n'
            '  set out to out & (priority of r as string) & linefeed\n'
            '  set dd to (due date of r)\n'
            '  if dd is missing value then\n'
            '    set out to out & "none" & linefeed\n'
            '  else\n'
            '    set out to out & (year of dd as integer) & "," & '
            '(month of dd as integer) & "," & (day of dd) & "," & '
            '(hours of dd) & "," & (minutes of dd) & linefeed\n'
            '  end if\n'
            '  set ad to (allday due date of r)\n'
            '  if ad is missing value then\n'
            '    set out to out & "none" & linefeed\n'
            '  else\n'
            '    set out to out & (year of ad as integer) & "," & '
            '(month of ad as integer) & "," & (day of ad) & linefeed\n'
            '  end if\n'
            '  return out\n'
            'end tell'
        )
        if out.strip() == "NOTFOUND":
            raise RemindersError(f"写入后按 id 找不到条目 {reminder_id}")
        got = out.splitlines()
        if len(got) < 4:
            raise RemindersError(f"读回字段格式异常：{out!r}")
        got_flagged = got[0].strip().lower() == "true"
        try:
            got_priority = int(got[1].strip())
        except ValueError:
            got_priority = -1
        if got_flagged != bool(flagged):
            raise RemindersError(
                f"旗标没写进去：要求 {flagged}，读回 {got_flagged}（{reminder_id}）")
        if got_priority != int(priority):
            raise RemindersError(
                f"优先级没写进去：要求 {priority}，读回 {got_priority}（{reminder_id}）")
        if due is not None:
            want = due.replace(second=0, microsecond=0)
            raw = got[3].strip() if allday_due else got[2].strip()
            got_d = _parse_due(raw)
            if got_d is None:
                raise RemindersError(
                    f"到期日没写进去：要求 {want}，读回 {raw!r}（{reminder_id}）")
            same = (got_d.date() == want.date() if allday_due else got_d == want)
            if not same:
                raise RemindersError(
                    f"到期日不一致：要求 {want}，读回 {got_d}（{reminder_id}）")

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


def make_key(text: str, note_id: str = "") -> str:
    """
    生成去重键：**按内容 + 所属日期**。

    两个约束（都来自实测）：

    ① 内容部分必须用**指纹**而非原样文字。
       同一条待办在不同阶段文字不同：
           本页原始：`勘察表盖章`
           顺延过来：`勘察表盖章 ⟳10-02`
       用原样文字会让它们变成两件不同的事，顺延一次就在提醒事项里
       重建一份（实测踩到：出现"已完成的旧条目 + 未完成的新条目"两份）。

    ② 必须**带上所属日期**。
       曾把键做成"跨天同一条"，结果是 10-03 的条目映射到 10-02 那条已完成，
       于是今天**没有可打钩的东西**。而每天的待办其实是独立的一次：
           10-02 的「交电费」完成了 —— 那是 10-02 的记录
           10-03 的「交电费」又要做 —— 该是一条新的、未完成的
       所以键 = 指纹 + 日期。同一天内幂等（重复跑不重建），
       跨天则各自独立（今天永远有得打钩）。

    note_id 里含日期信息（`.../ICNote/p69` → 对应某一天的页面），
    所以直接用它做日期维度。
    """
    import hashlib
    fp = _fingerprint(text)
    scope = note_id or ""
    h = hashlib.sha256(f"{fp}|{scope}".encode("utf-8")).hexdigest()[:16]
    return f"{KEY_PREFIX}{h}"


def _fingerprint(text: str) -> str:
    """内容指纹：复用解析层的实现，避免"同一件事两份实现"。"""
    try:
        import parse as _p
        return _p.content_fingerprint(text)
    except Exception:
        # 兜底：解析层不可用时退化为简单清理（宁可稍宽也不要崩）
        import re as _re
        t = _re.sub(r"⟳\s*\d{2}-\d{2}", " ", text)
        return t.replace("⟳", " ").replace("- [ ]", " ").replace("[ ]", " ").strip()


def key_of(reminder: Reminder) -> str | None:
    """从 body 里取出去重键。"""
    if reminder.body and reminder.body.startswith(KEY_PREFIX):
        return reminder.body.split()[0].strip()
    return None


# ── CLI

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="提醒事项访问（限定一个列表：配置指定，或系统的默认列表）")
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
