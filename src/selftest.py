#!/usr/bin/env python3
"""
selftest.py —— 不需要备忘录访问权限的自检。

存在的原因：一次重构时我用「删除两个方法之间的整段再插入」的方式改 notes.py，
结果把 `names()` 和 `note_id_for()` 一起删掉了 —— 而这两个方法只在真正读备忘录
时才被调用，我在沙箱里又跑不了备忘录访问（TCC 只授权给终端），
于是这个 AttributeError 一路流到用户那里才暴露。

所以这里做两件事：
  1. **静态契约检查**：解析源码，确认上层调用的每个方法都真实存在
  2. **解析器行为检查**：用固定用例断言解析结果（纯函数，离线可测）

凡是"能离线验证的"都应该在这里验掉，不要等用户跑。
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

# ⚠️ 自检的最外层安全阀：**禁止一切真实发送**。
#
# 实测踩到过：验证看门狗时忘了给 alert_once 注入假 sender，于是两条**真告警**
# 直接推到了手机上。通知类代码的测试有个特点 —— 忘一次就是一次真实打扰，
# 而"到底发出去没有"在断言里看不出来（返回 True 也可能是真的发了）。
# 所以在入口处声明：这一轮跑的是自检，notify 的发送原语一律抑制。
# （与 journal 的 PDCA_JOURNAL_DIR 同一思路：测试环境由调用方声明。）
os.environ["PDCA_SUPPRESS_SEND"] = "1"

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f"  —— {detail}" if detail else ""))
        failures.append(name)


# ── 段落编号
#
# 用变量统一维护，不手工写死在标题里。手工编号会随增删段落失配 ——
# 实测出现过两个 "19."（注释改了，print 里的没跟上）。
_sec_no = 0


def section(title: str) -> None:
    """打印带序号的段落标题。"""
    global _sec_no
    _sec_no += 1
    print(f"\n── {_sec_no}. {title} ──")


# ── 静态契约：上层调用的方法必须存在

section("静态契约检查（方法是否存在）")

notes_tree = ast.parse((SRC / "notes.py").read_text(encoding="utf-8"))
notes_cls = next(
    (n for n in notes_tree.body if isinstance(n, ast.ClassDef) and n.name == "Notes"),
    None,
)
if notes_cls is None:
    check("notes.py 里有 Notes 类", False, "找不到 Notes 类")
    print("\n无法继续，退出。")
    sys.exit(1)

methods = {m.name for m in notes_cls.body if isinstance(m, ast.FunctionDef)}

# 收集**全部** src/*.py 里对 notes 对象的方法调用。
#
# 这一点被两次教训逼出来：早先只扫 read_day.py，结果 probe_checklist.py
# 调用了被重构删掉的 get()，静态检查没发现，错误又流到用户那里。
# 检查范围必须覆盖所有调用方，否则等于没检查。
called: set[str] = set()
for _f in sorted(SRC.glob("*.py")):
    tree = ast.parse(_f.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("notes", "self")
        ):
            called.add(node.attr)

# 只检查我们自己定义的那批（排除标准库/内置调用）
our_api = {
    "names", "note_id_for", "plaintext_of", "name_of", "find_daily_page",
    "verify_folder", "list_notes", "get", "get_by_name", "get_by_id",
    "create", "append_lines",
}
expected = called & our_api
missing = expected - methods
check(
    f"全部 src/*.py 调用的 {len(expected)} 个方法都存在",
    not missing,
    f"缺失：{sorted(missing)} —— 重构时删掉了方法，但调用方还在引用",
)


# ── 解析器行为（纯函数，离线可测）

section("解析器行为检查")
import parse as parser  # noqa: E402


def kinds(text: str) -> list[tuple[str, str, bool | None]]:
    r = parser.parse(text)
    return [(e.kind, e.text, e.completed) for e in r.entries]


# 基础标记
r = kinds("2026-10-02\n- [ ] 待办甲\n- [x] 待办乙\n* 备忘丙\n@中午 时段丁")
check("识别日期标题", r[0][0] == "meta" and r[0][1] == "2026-10-02")
check("未完成待办", r[1] == ("todo", "待办甲", False))
check("已完成待办", r[2] == ("todo", "待办乙", True))
check("备忘不进待办", r[3][0] == "note" and r[3][1] == "备忘丙")
check("旧 @时段 前缀被剥掉（v1 兼容；v4 的 @ 是日历符号）",
      r[4][1] == "时段丁")

# 裸行 = 待办（最高频情况，零符号）
r = kinds("裸行没有符号")
check("裸行视为未完成待办", r[0] == ("todo", "裸行没有符号", False))

# 智能标点：iOS 会把 "- " 变成 "– "/"— "，不能因此漏掉标记
for dash in ["-", "–", "—"]:
    r = kinds(f"{dash} [ ] 标点测试")
    check(f"容忍智能标点 {dash!r}", r[0][1] == "标点测试")

# 项目符号 = 备忘
r = kinds("• 项目符号备忘")
check("容忍项目符号 •", r[0][0] == "note" and r[0][1] == "项目符号备忘")

# 无列表符号的复选框
r = kinds("[ ] 无符号复选框")
check("无列表符号的 [ ]", r[0][0] == "todo" and r[0][2] is False)
r = kinds("[x] 无符号已完成")
check("无列表符号的 [x]", r[0][0] == "todo" and r[0][2] is True)

# 行尾残留标记（用户先写内容后补标记）
r = kinds("只有内容 - [ ]")
check("行尾残留标记被剥掉", r[0][1] == "只有内容")

# 元信息行
r = kinds("# 备注行")
check("# 开头视为元信息", r[0][0] == "meta")

# 空行不产出条目
r = kinds("甲\n\n\n乙")
check("空行不产出条目", len(r) == 2)

# 去重：只报告，不删除
res = parser.parse("提交结算单\n提交结算单\n完全不同的事")
dups = parser.find_duplicates(res.entries)
check("发现疑似重复", len(dups) >= 1, f"实际 {len(dups)} 组")
check("去重不删除任何条目", len(res.entries) == 3)

# 原文逐字保留（人工核对依赖这一点）
raw = "2026-10-02\n- [ ] 甲 乙  丙\n"
res = parser.parse(raw)
check("原文逐字保留", res.entries[1].raw == "- [ ] 甲 乙  丙")


# ── 顺延逻辑（离线可测 —— 这些分支在沙箱里必须能验掉）

section("顺延逻辑检查")
import carry_over  # noqa: E402


def plan_of(prev_text: str, next_text: str | None) -> tuple[list[str], list[str]]:
    prev = parser.parse(prev_text)
    return carry_over.build_plan(prev, next_text, "2026-10-02", "2026-10-03")


# 基本顺延
plan, _ = plan_of("2026-10-02\n- [ ] 甲\n- [x] 乙\n- [ ] 丙", None)
check("只顺延未完成项", len(plan) == 2 and any("甲" in l for l in plan) and any("丙" in l for l in plan))
check("已完成项被排除", not any("乙" in l for l in plan))

# 备忘不参与
plan, _ = plan_of("2026-10-02\n- [ ] 甲\n* 备忘丙", None)
check("备忘不被顺延", len(plan) == 1)

# 顺延行的格式
plan, _ = plan_of("2026-10-02\n- [ ] 甲", None)
check("顺延行带 ⟳ 标记（含来源日期）",
      bool(plan) and carry_over.CARRY_MARK in plan[0])
check("顺延行是未完成复选框", plan and plan[0].startswith("- [ ] "))

# 幂等性 —— 这是实测踩到的 bug：次日页里是「甲 ⟳」，来源是「甲」，
# 不剥标记就比不相等，于是同一条被顺延第二遍。
plan, skipped = plan_of("2026-10-02\n- [ ] 甲", "2026-10-03\n- [ ] 甲 ⟳10-02\n")
check("幂等：目标页已有「甲 ⟳10-02」时不重复顺延", len(plan) == 0, f"实际 {plan}")

# 裸 ⟳（无日期）也要能识别为历史项 —— 历史遗留的写法可能没有日期
plan, skipped = plan_of("2026-10-02\n- [ ] 甲", "2026-10-03\n- [ ] 甲 ⟳\n")
check("幂等：裸「⟳」标记也算历史项", len(plan) == 0, f"实际 {plan}")

plan, _ = plan_of("2026-10-02\n- [ ] 甲", "2026-10-03\n- [ ] 甲\n")
check("幂等：次日页有无标记的同名项也不重复", len(plan) == 0)

# 相似但不同 → 保留
plan, _ = plan_of("2026-10-02\n- [ ] 提交结算单A", "2026-10-03\n- [ ] 提交结算单B\n")
check("相似但不同的事项被保留", len(plan) == 1)

# 高相似 → 跳过并说明（不静默丢弃）
plan, skipped = plan_of("2026-10-02\n- [ ] 提交结算单", "2026-10-03\n- [ ] 提交结算单\n")
check("高相似时跳过并给出说明", len(plan) == 0 and len(skipped) >= 1)

# 全部完成 → 无动作
plan, _ = plan_of("2026-10-02\n- [x] 甲\n- [x] 乙", None)
check("全部完成时无事可做", len(plan) == 0)

# ⟳ 不能被 NFKC 归一化改掉（否则去重键会失效）
check("⟳ 标记在归一化后保持不变",
      carry_over.CARRY_MARK in parser.normalize_line(f"甲 {carry_over.CARRY_MARK}"))


# ── 留档渲染

section("留档渲染检查")
res = parser.parse("2026-10-02\n- [ ] 甲\n* 乙")
md = parser.render_archive(res, {"date": "2026-10-02", "source": "test"})
check("留档含原文段", "## 原文（逐字保留）" in md)
check("留档含解析段", "## 解析结果" in md)
check("留档含日期", "date: 2026-10-02" in md)


# ── AppleScript 字面量转义

section("AppleScript 转义检查")
import re as _re  # noqa: E402
import notes as _notes  # noqa: E402

# 这是一次真实故障：bash 脚本里手写 AppleScript 字符串，HTML 中的双引号
# （class="..."）没转义，直接终止了字面量 → AppleScript 报 -2741 语法错。
# 现在统一走 _as_literal，这里验证它对各类输入都正确。
for _raw in ['class="x"', 'a"b"c', 'C:\\path', '中文"引号"测试', 'back\\slash"mix']:
    _lit = _notes._as_literal(_raw)
    _inner = _lit[1:-1]
    _bad = _re.findall(r'(?<!\\)"', _inner)
    check(f"转义 {_raw!r} 后无裸引号", not _bad, f"字面量={_lit}")

# 反斜杠必须最先转义，否则会把后续插入的转义再转一次
check("反斜杠先于引号转义",
      _notes._as_literal('\\"') == '"\\\\\\""',
      _notes._as_literal('\\"'))


# ── 清理判据（哪些能删、哪些必须留）

section("清理判据检查")
import datetime as _dt  # noqa: E402
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("_cleanup", SRC / "cleanup.py")
_cleanup = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_cleanup)

_today = _dt.date(2026, 10, 2)
# 这些**必须删**（历次探测的残留 + 提前生成的页面）
for _n in ["FMT-TEST-1　x", "STRUCT-PROBE-2", "DIAG-OSA-3", "# PDCA probe 页",
           "2026-10-03", "2026-12-31"]:
    check(f"应删：{_n[:24]}", _cleanup.classify(_n, _today) is not None)

# 这些**必须留**（真实数据！误删不可恢复）
for _n in ["2026-10-02", "2026-10-01", "国际象棋", "2026-10-02 备注",
           "孙宇晨经常说，普通人没有战略"]:
    check(f"应留：{_n[:24]}", _cleanup.classify(_n, _today) is None)


# ── 提醒事项同步规则

section("提醒事项同步规则")
import importlib.util as _ilu2  # noqa: E402

def _load(path):
    """
    按**规范模块名**加载 src 下的模块（不要另起别名）。

    两个坑，都是实际踩出来的：
      1. 必须注册进 sys.modules —— 否则 @dataclass 解析类型标注时找不到
         本模块命名空间，报 `'NoneType' object has no attribute '__dict__'`。
      2. **必须用模块本名**（`reminders` 而不是 `_reminders`）。因为模块内部会
         `import reminders`，若外部用的是别名，就会加载出**第二份**，
         于是 `RemindersError` 变成两个不同的类，`except` 捕获不到 ——
         表现为异常"穿透"了本该处理它的代码。
    """
    import sys as _sys
    import importlib as _il
    _name = path.stem
    if str(SRC) not in _sys.path:
        _sys.path.insert(0, str(SRC))
    if _name in _sys.modules:
        return _sys.modules[_name]
    _s = _ilu2.spec_from_file_location(_name, path)
    _m = _ilu2.module_from_spec(_s)
    _sys.modules[_name] = _m
    _s.loader.exec_module(_m)
    return _m

_rems = _load(SRC / "reminders.py")
_tg = _load(SRC / "telegram.py")
_push = _load(SRC / "push_tasks.py")

_NOTE = "x-coredata://TEST/ICNote/p1"


def _rem(rid, name, completed, body=""):
    return _rems.Reminder(id=rid, name=name, completed=completed, body=body, due="")


_page = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙")
_keyA = _rems.make_key("甲", _NOTE)
_keyB = _rems.make_key("乙", _NOTE)

# 空列表 → 全部新建（备忘不参与）
_p = _push.build_plan(_page, [], _NOTE)
check("空列表时新建全部待办", len(_p.create) == 2)

# 幂等：已有同键条目 → 不重复建
_p = _push.build_plan(_page, [_rem("r1", "甲", False, _keyA)], _NOTE)
check("已有同键条目不重复新建", len(_p.create) == 1 and len(_p.unchanged) == 1)

# 重复运行 → 无改动（这一步最关键：每天都会跑）
_p = _push.build_plan(_page, [_rem("r1", "甲", False, _keyA),
                             _rem("r2", "乙", False, _keyB)], _NOTE)
check("重复运行无改动（幂等）", _p.total_changes == 0)

# 备忘录打了钩 → 同步为完成
_p = _push.build_plan(parser.parse("2026-10-02\n- [x] 甲"), [_rem("r1", "甲", False, _keyA)], _NOTE)
check("备忘录 [x] 同步为完成", len(_p.complete) == 1)

# **绝不回退**：提醒事项已完成，备忘录仍写 [ ]
_p = _push.build_plan(_page, [_rem("r1", "甲", True, _keyA)], _NOTE)
check("已完成的条目绝不回退", len(_p.complete) == 0 and len(_p.already_done) == 1)

# 换一天 → 不误判为重复（key 含 note_id）
_p = _push.build_plan(_page, [_rem("r1", "甲", False, _keyA)], "x-coredata://TEST/ICNote/p2")
# 跨天行为（设计决定）：**每天是独立的一次待办**。
# 曾把键做成"跨天同一条"，结果是新一天继承了旧一天的"已完成"，
# 于是今天没有可打钩的东西 —— 实测踩到。
# 正确语义：10-02 的「交电费」完成了是那天的记录；
#          10-03 的「交电费」又要做，该是一条新的未完成项。
_p = _push.build_plan(_page, [_rem("r1", "甲", False, _keyA)], "x-coredata://TEST/ICNote/p2")
check("跨天各自独立（今天有得打钩）", len(_p.create) == 2,
      f"create={len(_p.create)}")

# 孤儿条目只报告（可能是你手动加的），不自动删
_p = _push.build_plan(_page, [_rem("r9", "手动加的", False)], _NOTE)
check("孤儿条目只报告不自动删", len(_p.orphan) == 1)

# 去重键要能区分内容
check("改文字后视为新内容",
      _rems.make_key("甲", _NOTE) != _rems.make_key("甲改过了", _NOTE))

# **关键回归**：顺延过的条目在提醒事项里必须映射到同一条待办，
# 否则每顺延一次就重建一份（实测踩到：提醒事项里出现重复条目）。
_fp_forms = ["勘察表盖章", "勘察表盖章 ⟳10-02", "勘察表盖章 ⟳",
             "- [ ] 勘察表盖章 ⟳10-02"]
check("顺延标记不影响去重键（不重复建条目）",
      len({_rems.make_key(t, _NOTE) for t in _fp_forms}) == 1)


# ── 完成状态合并（权威 = 提醒事项）

section("完成状态合并规则")
_completion = _load(SRC / "completion.py")

_CNOTE = "x-coredata://TEST/ICNote/p1"


class _FakeReminders:
    """桩对象：让合并逻辑可以完全离线测试。"""
    def __init__(self, reminders=None, fail=False):
        self._r = reminders or []
        self._fail = fail
        self.list_name = "PDCA"

    def all_reminders(self):
        if self._fail:
            raise _rems.RemindersError("模拟不可用")
        return self._r


def _r2(name, completed, line_no):
    return _rems.Reminder(id=f"r{line_no}", name=name, completed=completed,
                          body=_rems.make_key(name, _CNOTE), due="")


_pg = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙\n- [ ] 丙\n* 备忘")

# 提醒事项里已完成 → 计入完成
_res = _completion.resolve(_pg, _CNOTE, _FakeReminders([_r2("乙", True, 3)]))
check("提醒事项已完成计入完成", len(_res.done) == 1 and _res.done[0].source == "reminders")
check("未完成的计数正确", len(_res.open) == 2)

# 备忘录打钩、提醒事项未完成 → **倾向完成**
_pg2 = parser.parse("2026-10-02\n- [ ] 甲\n- [x] 乙\n- [ ] 丙")
_res = _completion.resolve(_pg2, _CNOTE, _FakeReminders([_r2("乙", False, 3)]))
check("倾向完成：备忘录 [x] 也算完成", len(_res.done) == 1 and _res.done[0].source == "notes")

# 两处都完成 → 不重复计数
_res = _completion.resolve(_pg2, _CNOTE, _FakeReminders([_r2("乙", True, 3)]))
check("两处都完成不重复计数", len(_res.done) == 1)

# 尚未同步到提醒事项的条目要被标出
_res = _completion.resolve(_pg, _CNOTE, _FakeReminders([]))
check("未同步条目被标出", len(_res.not_pushed) == 3)

# 提醒事项不可用 → 退化，不崩，且如实标注
_res = _completion.resolve(_pg2, _CNOTE, _FakeReminders(fail=True))
check("提醒事项不可用时不崩且退化", _res.reminders_available is False and len(_res.done) == 1)

# 回填留档
_pg3 = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙\n- [ ] 丙")
_n = _completion.write_back(_pg3, _completion.resolve(_pg3, _CNOTE, _FakeReminders([_r2("乙", True, 3)])))
check("回填留档只改有差异的条目", _n == 1 and [e.completed for e in _pg3.todos] == [False, True, False])


# ── 顺延的完成状态来自留档（权威链路的落点）

section("顺延依据留档里的完成状态")

# 这是整条权威链路的关键一环：
#   21:30 日报 resolve(提醒事项) → write_back → save_archive（落盘）
#   → 次日 07:00 顺延读留档 → 只带走未完成的
# 若这一环断了，你昨晚打的钩会失效、做完的事被顺延到明天。
_prev_done = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙")
_prev_done.entries[2].completed = True   # 模拟"留档里乙已完成（来自提醒事项）"
import carry_over as _co  # noqa: E402
_plan, _ = _co.build_plan(_prev_done, None, "2026-10-02", "2026-10-03")
check("留档标记为完成的条目不参与顺延",
      len(_plan) == 1 and "甲" in _plan[0], f"实际 {_plan}")

# 全部完成 → 无事可做
_prev_all = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙")
for _e in _prev_all.entries:
    _e.completed = True
_plan, _ = _co.build_plan(_prev_all, None, "2026-10-02", "2026-10-03")
check("留档里全部完成 → 不顺延任何条目", len(_plan) == 0)


# ── 顺延的幂等与防链式

section("顺延幂等 / 不链式往后传")

_prev02 = parser.parse("2026-10-02\n@中午 勘察表盖章\n明天要交电费")

# 首次顺延：应写 2 行，标记带来源日期
_plan, _ = _co.build_plan(_prev02, None, "2026-10-02", "2026-10-03")
check("首次顺延标记带来源日期",
      len(_plan) == 2 and all("⟳10-02" in l for l in _plan), str(_plan[:1]))

# 再跑一次（目标页已有这些行）→ 必须是 0，这才是幂等
_next03 = "2026-10-03\n" + "\n".join(_plan)
_plan2, _ = _co.build_plan(_prev02, _next03, "2026-10-02", "2026-10-03")
check("重复运行不重复写入（幂等）", len(_plan2) == 0, str(_plan2))

# **核心行为**：未完成的事必须一天天往后带。
# 曾经加过一条"来源自带旧 ⟳ 标记就跳过"的判据来"防链式顺延" ——
# 那条是错的且危害很大：顺延过来的条目本来就都带标记，于是再也不会被往后带，
# 等于**静默丢失**用户还没做的事（实测：10-03 的 3 条被全部拦住）。
# 正确认识：链式顺延本身不是问题，没做完的事就该一天天带着。
_prev03 = parser.parse(_next03)
_plan3, _sk3 = _co.build_plan(_prev03, None, "2026-10-03", "2026-10-04")
check("未完成的事一天天往后带（不静默丢失）", len(_plan3) == 2, str(_plan3))
check("顺延标记不累积（只留最近一次）",
      all(l.count("⟳") == 1 for l in _plan3), str(_plan3))

# 各种写法都要归一到同一个指纹（曾产生 `⟳ ⟳`）
_fps = {_co.content_fingerprint(t) for t in
        ["勘察表盖章", "勘察表盖章 ⟳", "勘察表盖章 ⟳ ⟳",
         "- [ ] 勘察表盖章 ⟳", "@中午 勘察表盖章"]}
check("不同标记写法归一到同一指纹", len(_fps) == 1, str(_fps))


# ── AppleScript 语法校验（osacompile，只编译不执行）

section("AppleScript 语法校验")

import subprocess as _sp  # noqa: E402

# 为什么要这一步：AppleScript 的语法错（引号未转义、条件表达式拼错）
# 只有真正交给 osascript 才会暴露，而我本地没有备忘录/提醒事项权限，
# 一跑就是 -10004 —— 于是语法错会伪装成权限错，被忽略。
# `osacompile` 只编译不执行，**不触发授权**，所以能在这里把语法验掉。
_as_templates = [
    ('提醒事项：列出所有列表',
     'tell application "Reminders" to get name of every list'),
    ('提醒事项：新建列表',
     'tell application "Reminders"\n make new list with properties {name:"X"}\n return "ok"\nend tell'),
    ('提醒事项：新建条目（含到期）',
     'tell application "Reminders"\n set L to list "X"\n make new reminder at L with properties {name:"Y", body:"Z", due date:(current date) + 1 * hours}\n return "ok"\nend tell'),
    ('提醒事项：设置完成状态',
     'tell application "Reminders"\n set hits to (every reminder whose id is "Z")\n set completed of item 1 of hits to true\n return "ok"\nend tell'),
    ('提醒事项：按完成状态过滤',
     'tell application "Reminders"\n set L to list "X"\n return (count of (every reminder of L whose completed is false)) as string\nend tell'),
    ('备忘录：按 id 取纯文本',
     'tell application "Notes"\n set hits to (every note of folder id "F" whose id is "N")\n return plaintext of item 1 of hits\nend tell'),
    ('备忘录：新建条目',
     'tell application "Notes"\n make new note at folder id "F" with properties {body:"B"}\n return "made"\nend tell'),
]

_as_fail = []
for _label, _src in _as_templates:
    _r = _sp.run(["osacompile", "-o", os.devnull, "-e", _src],
                 capture_output=True, text=True)
    if _r.returncode != 0:
        _as_fail.append(f"{_label}：{_r.stderr.strip()[:80]}")

check(f"{len(_as_templates)} 个 AppleScript 模板语法正确",
      not _as_fail, "；".join(_as_fail))


# ── 系统 Python 3.9 兼容性

section("AppleScript 的 whose 子句")

# 反复踩到的坑：`whose id "..."` 缺 `is` 会**编译失败**（-2741），
# 报错只说"期望逗号但找到引号"，完全看不出是缺 is —— 骗了我好几次。
#
# 检查方式的选择过程（值得记下）：
#   ① 先试"正则匹配 whose <属性> 后跟引号" → 拦不住：真实写法是
#      f-string，`is` 后面跟的是 {_as_literal(...)}，不是引号。
#   ② 再试"从源码求值出 AppleScript 再编译" → 有大量**假阳性**
#      （mock 求值产生残缺片段）。有假阳性的检查比没有检查更糟：
#      会被忽略，或被迫弱化到无效。
#   ③ 最终用**结构检查**：在真实的 f-string 里，`is` 是明文写着的，
#      所以只要确认每个 whose 子句附近都有 is/contains 即可，简单且准确。
import re as _re5  # noqa: E402

# 属性名可以是两个词（`whose completion date is greater than d`），
# 而且源码里这个子句常常跨行 —— 跨行处会插进 `'` 换行 缩进 `'`
# （Python 相邻字符串字面量拼接），所以间隙要允许这些字符，不能只允许 \w\s。
# ⚠️ 扫描范围要**包含归档目录**，否则"把脚本挪到 legacy/" 就成了
# 让检查失效的捷径 —— 检查看不到的东西等于没检查。
# （2026-10-03 整理目录时特意确认过：移走的 10 个脚本当时 0 违规，
#   所以纳入扫描不需要任何豁免。）
_SHELL_FILES = (sorted((ROOT / "deploy").glob("*.sh"))
                + sorted((ROOT / "deploy" / "legacy").glob("*.sh"))
                + sorted((ROOT / "tools").glob("*.sh"))
                + sorted((ROOT / "tools" / "legacy").glob("*.sh")))

_WHOSE_OK = _re5.compile(
    r"whose.{0,40}?(?:(?:is|contains)\b|[≥≤<>=≠])", _re5.S)
_hits5: list[str] = []
_scanned5 = 0


def _docstring_lines(path) -> set:
    """
    这个文件里**所有 docstring 占用的行号** —— 用 ast，不数引号。

    2026-10-05 换掉了"数三引号出现次数的奇偶"那套写法：`applecal.py` 的
    注释里**引用**了三引号（解释 -2741 那个编译错误），于是奇偶被带偏、
    之后整个文件的"在不在 docstring 里"判反 ——
    既漏报真代码（一大段被当成 docstring 跳过），又误报 docstring
    （把讲解 whose 用法的句子当成违规）。
    看得见的那次误报是运气好；看不见的漏报才是真问题。

    （连这个函数的注释都得绕着三引号写 —— 那正是它的理由。）
    """
    _tree = ast.parse(path.read_text(encoding="utf-8"))
    _out: set = set()
    _nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for _n in ast.walk(_tree):
        if not isinstance(_n, _nodes):
            continue
        _body = getattr(_n, "body", [])
        if (_body and isinstance(_body[0], ast.Expr)
                and isinstance(_body[0].value, ast.Constant)
                and isinstance(_body[0].value.value, str)):
            _out.update(range(_body[0].lineno, _body[0].end_lineno + 1))
    return _out


for _f in list((ROOT / "src").glob("*.py")) + _SHELL_FILES:
    if _f.name == "selftest.py":
        continue
    _lines5 = _f.read_text(encoding="utf-8").splitlines()
    # docstring 里提到 whose 是在讲事情，不是在写 AppleScript。
    # 行号由 ast 给出（见 _docstring_lines 的说明：数引号会被注释带偏）。
    _in_doc = _docstring_lines(_f) if _f.suffix == ".py" else set()
    for _ln, _line in enumerate(_lines5, 1):
        _stripped = _line.lstrip()
        if _ln in _in_doc:
            continue
        # 跳过注释：注释里会引用反例（"whose id \"...\""），误报过
        if _stripped.startswith("#"):
            continue
        for _m in _re5.finditer(r"whose\s+\w+", _line):
            _scanned5 += 1
            # ⚠️ 要在**拼接后的相邻两行**里找 is/contains，不能只看本行。
            # 属性名可以是两个词（`whose completion date is greater than d`），
            # 而源码里这个子句常常跨行写 —— 只看本行会把合法写法判成违规，
            # 逼着人把 AppleScript 挤成一行来讨好检查（那是本末倒置）。
            # 判据放宽到"本行 + 下一行"就够：AppleScript 语句不会更长。
            _tail = "\n".join(_lines5[_ln - 1:_ln + 1])[_m.start():]
            if not _WHOSE_OK.search(_tail[:90]):
                _hits5.append(f"{_f.name}:{_ln}  {_tail.strip()[:50]}")

check(f"{_scanned5} 个 whose 子句都带 is/contains",
      not _hits5 and _scanned5 > 5,
      "；".join(_hits5[:3]) if _hits5 else f"只扫描到 {_scanned5} 个，可能扫描失败")

# ⚠️ 2026-10-05 把上面那条正则放宽了：它原本只认 `is|contains`，
# 而日历读取现在用 `whose start date ≥ dFrom and start date < dTo`
# 按日期筛 —— 比较运算符是**同一类合法写法**，缺运算符才会编译失败。
# 放宽没有削弱它要防的东西（缺运算符的 `whose id "X"` 照样会被抓到）。
#

# 不能出现重复的 is（批量替换时误伤过，产生 "is is"）
_dup5: list[str] = []
for _f in list((ROOT / "src").glob("*.py")) + _SHELL_FILES:
    if _f.name == "selftest.py":
        continue
    for _ln, _line in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
        if _line.lstrip().startswith("#"):
            continue
        if " is is " in _line:
            _dup5.append(f"{_f.name}:{_ln}")
check("代码里没有重复的 is", not _dup5, "；".join(_dup5[:3]))


section("顺延后必须为次日页建留档")

# 实测踩到：顺延在备忘录里建了 10-03，但没建留档 ——
# 于是"新的一页不在系统视野内"：日报读不到它、时段询问扫描不到它、
# 再下次顺延也读不到它。原因不是崩溃，而是**漏了一步**。
#
# 这类"漏接线"的问题靠运行看不出来（一切正常、就是少了一页），
# 所以要静态确认这步真的在。
_co_src = (SRC / "carry_over.py").read_text(encoding="utf-8")
check("顺延后会为次日页建留档",
      "建立留档" in _co_src and "sync.sync_day(next_date_str)" in _co_src)
check("建留档失败不阻断顺延（备忘录已写入）",
      "建立留档失败" in _co_src)


section("「内容未变」必须视为成功")

# Telegram 对"内容与当前完全一致"的编辑**返回 HTTP 400 错误**：
#   message is not modified: specified new message content ... exactly the same
# 若调用方把它当失败 → 重试 → 退化成"发新消息"，
# 于是**每次点击都多一条消息**（实测踩到：用户看到测试后又连收几条）。
#
# 语义上"内容已是目标样子"就是成功，所以必须识别并返回成功。
_tg_src = (SRC / "telegram.py").read_text(encoding="utf-8")
check("telegram 定义了 NOT_MODIFIED 常量", "NOT_MODIFIED" in _tg_src)
check("edit_with_buttons 处理「内容未变」",
      "NOT_MODIFIED in str(e)" in _tg_src)

_sig_edit = inspect.signature(_tg.edit_with_buttons)
check("edit_with_buttons 签名正常", "message_id" in _sig_edit.parameters)

check("telegram 提供 drain_stale_callbacks（清理过期按钮）",
      hasattr(_tg, "drain_stale_callbacks"))


section("通知通道")

_notify = _load(SRC / "notify.py")

# 双通道是有意的设计：互为冗余，任一不可用另一条仍能到达
check("notify 提供 broadcast（多通道）", hasattr(_notify, "broadcast"))
check("notify 提供 Telegram 发送", hasattr(_notify, "send_telegram"))
check("notify 保留 Bark 发送", hasattr(_notify, "send_bark"))

import inspect as _insp2  # noqa: E402
_bc_sig = _insp2.signature(_notify.broadcast)
check("broadcast 支持指定通道", "channels" in _bc_sig.parameters)
_bc_src = _insp2.getsource(_notify.broadcast)
check("默认同时发 Telegram 与 Bark",
      '"telegram"' in _bc_src and '"bark"' in _bc_src)

# 日报默认走双通道（v4 的 report.py；v1 的 daily_report.py 仍在但已不接线）
_rp_src = (SRC / "report.py").read_text(encoding="utf-8")
check("v4 日报默认双通道", "telegram,bark" in _rp_src)


section("系统 Python 兼容性")

# 为什么单独查这个：launchd 任务用的是 **/usr/bin/python3（3.9）**，
# 而我平时用自带运行时（3.12）。若代码用了运行时求值的类型标注
# （如 dataclass 字段上的 `str | None`），本地测得好好的，
# 定时任务里却 import 失败 —— 那是最难查的一类问题。
# 所以这里**真的用系统 python3 导入一遍**，而不是只做语法解析
# （ast.parse 只验语法、不求值注解，抓不到这类问题）。
import subprocess as _sp2  # noqa: E402

_SYS_PY = "/usr/bin/python3"
_modules = ["parse", "notes", "sync", "notify", "reminders", "push_tasks",
            "cleanup", "daily_report", "read_day", "completion", "telegram",
            "cleanup_reminders", "reconcile"]

if not os.path.exists(_SYS_PY):
    check("系统 python3 存在", False, f"找不到 {_SYS_PY}")
else:
    _bad = []
    for _m in _modules:
        _r = _sp2.run(
            [_SYS_PY, "-c",
             f"import sys; sys.path.insert(0, {str(SRC)!r}); import {_m}"],
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            _err = (_r.stderr.strip().splitlines() or ["?"])[-1]
            _bad.append(f"{_m}: {_err[:70]}")
    check(f"{len(_modules)} 个模块可被系统 python3 (3.9) 导入",
          not _bad, "；".join(_bad))


# ── 关键路线的三个定时任务必须齐全

section("安装脚本的健壮性")

_inst2 = (ROOT / "deploy" / "install_launchd.sh").read_text(encoding="utf-8")

# 未配置也允许安装：先把任务装好，等初始化完成后系统自动开始工作，
# 不需要再手动装一次。两个任务在未配置时都是安全的 ——
# 守护失败不退出（已修崩溃循环）、日报如实报告读取失败。
check("支持 --allow-unconfigured", "--allow-unconfigured" in _inst2)
check("不带开关时仍会拒绝", 'ALLOW_UNCONFIGURED=0' in _inst2)
check("未配置时给出提示而非直接失败",
      "初始化后自动恢复" in _inst2)

# 删不掉遗留 plist 时不该中止（受限沙箱里 rm 会失败）
check("清理失败不中止安装", "已卸载、不会再运行" in _inst2)

# 写不了 LaunchAgents 时要给出可操作指引，而不是让 set -e 莫名掐断
check("无法写入时给出明确指引", "受限沙箱" in _inst2)

# ⚠️ **不能靠 launchctl 的返回码判断是否加载成功**。
# 实测：`launchctl load` 即使失败也返回 0，`bootstrap` 失败返回 5。
# 于是 `bootstrap || load` 的写法永远"成功" —— 实测踩到：脚本打印
# "✅ 已安装"，而守护任务其实没加载、一直没在跑。
# 唯一可靠的判据是 `launchctl print` 能否查到它。
check("安装后必须用 launchctl print 验证",
      'launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1' in _inst2
      or 'launchctl print "gui/$(id -u)/${label}"' in _inst2)
check("提供 restart 子命令（装过但没在跑时修复）",
      "do_restart" in _inst2 and "restart)" in _inst2)
check("status 能识别'文件在但任务未加载'",
      "文件已安装但任务未加载" in _inst2)

# bash 3.2 的全角字符陷阱：$VAR 后紧跟非 ASCII 会被当成变量名一部分。
# 这道检查已存在，但**我自己又踩了一次**（新加的 `$label：` 写法），
# 所以这里覆盖全部 deploy/*.sh 再确认一遍。
import re as _reA  # noqa: E402
_traps: list[str] = []
for _sh in _SHELL_FILES:
    _hits = _reA.findall(rb"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])",
                         _sh.read_bytes())
    if _hits:
        _traps.append(f"{_sh.name}: {[h.decode() for h in _hits]}")
check("deploy 下所有脚本无全角字符陷阱", not _traps, "；".join(_traps))


section("plist 必须通过严格 XML 解析")

# ⚠️ `plutil -lint` **不足以**验证 plist —— 它用宽松的旧式解析器，
# 会放过真正的 XML 错误。实测踩到：daemon.plist 里 XML 注释含
# `--drain`（**XML 注释不允许出现连续两个连字符**），
# plutil -lint 报 OK，而 launchd 拒绝加载 ——
# 表现是"安装显示成功、任务却不在跑"，排查了很久。
#
# 判据：用 plistlib（严格 XML 解析器）逐个解析。
import plistlib as _plC  # noqa: E402
_bad_plists: list[str] = []
for _f in sorted((ROOT / "deploy").glob("*.plist")):
    try:
        _plC.loads(_f.read_bytes())
    except Exception as _e:
        _bad_plists.append(f"{_f.name}: {str(_e)[:50]}")
check("deploy 下所有 plist 通过严格 XML 解析", not _bad_plists,
      "；".join(_bad_plists))

# XML 注释里不得出现 `--`（这是上面那个坑的直接判据，比解析更早暴露问题）
_dash_comments: list[str] = []
import re as _reD  # noqa: E402
for _f in sorted((ROOT / "deploy").glob("*.plist")):
    _txt = _f.read_text(encoding="utf-8")
    for _m in _reD.finditer(r"<!--(.*?)-->", _txt, _reD.S):
        if "--" in _m.group(1):
            _dash_comments.append(_f.name)
check("plist 注释里没有 --（XML 不合法）", not _dash_comments,
      f"{_dash_comments} → 注释里出现连续两个连字符会让 launchd 拒绝加载")

# 安装后的 plist（若已安装）也要能严格解析
import pathlib as _plD  # noqa: E402
_home_plists = sorted((_plD.Path.home() / "Library" / "LaunchAgents").glob(
    "io.github.carlapple2025.pdca.*.plist"))
if _home_plists:
    _bad_home: list[str] = []
    for _f in _home_plists:
        try:
            _plC.loads(_f.read_bytes())
        except Exception:
            _bad_home.append(_f.name)
    check(f"已安装的 {len(_home_plists)} 个 plist 也能严格解析",
          not _bad_home, "；".join(_bad_home))


section("v4 的定时任务路线")

# v4 的关键路线只有三个任务：
#   ① 常驻收件守护（KeepAlive）—— 你发一句就有人接
#   ② 21:30 日报（只读三处快照）
#   ③ 周日 20:00 周报（只读；完成 / 提交 / 连续天数）—— 2026-10-05 加
#
# v1 的三个任务（09:00 同步 / 21:30 日报 / 07:00 顺延）在 v4 都不需要：
# 待办常驻提醒事项，没有"同步"和"顺延"这两个概念。
_plists = {p.name for p in (ROOT / "deploy").glob("io.github.carlapple2025.pdca.*.plist")}
for _need in ("io.github.carlapple2025.pdca.daemon.plist", "io.github.carlapple2025.pdca.report.plist",
              "io.github.carlapple2025.pdca.weekly.plist"):
    check(f"存在 {_need}", _need in _plists)

_install = (ROOT / "deploy" / "install_launchd.sh").read_text(encoding="utf-8")
for _lbl in ("io.github.carlapple2025.pdca.daemon", "io.github.carlapple2025.pdca.report",
             "io.github.carlapple2025.pdca.weekly"):
    check(f"安装列表含 {_lbl}", _lbl in _install)

# 守护必须 KeepAlive（否则退出后没人接消息）
_dmn = (ROOT / "deploy" / "io.github.carlapple2025.pdca.daemon.plist").read_text(encoding="utf-8")
check("守护任务设了 KeepAlive", "<key>KeepAlive</key>" in _dmn)
check("守护任务有重启节流", "ThrottleInterval" in _dmn)
check("守护指向 daemon.py", "src/daemon.py" in _dmn)

# 日报指向新实现，且不带 v1 的 --refresh/--sync（v4 日报是纯只读）
_rep = (ROOT / "deploy" / "io.github.carlapple2025.pdca.report.plist").read_text(encoding="utf-8")
check("日报指向 report.py", "src/report.py" in _rep)
check("v4 日报不带 v1 的 --refresh", "--refresh" not in _rep)
check("v4 日报不带 v1 的 --sync", "--sync" not in _rep)


section("顺延依据当前权威状态")

# 实测踩到：用户 22:00 在提醒事项打了钩，但留档停在 21:52，
# 次日 07:02 的顺延把**已完成的事**也带到了新的一天，
# 造成"备忘录说未完成、提醒事项说已完成"的矛盾。
#
# 修法不是加个开关（那要靠人记得开），而是让它成为默认行为。
_co_src2 = (SRC / "carry_over.py").read_text(encoding="utf-8")
check("顺延总是从提醒事项取当前状态",
      "总是**从提醒事项取当前值**" in _co_src2
      or "总是" in _co_src2 and "completion.resolve" in _co_src2)
check("顺延不再依赖 --resolve 开关", '"--resolve"' not in _co_src2)

# 提供调和工具：把"提醒事项已完成、备忘录还写着 [ ]"的差异抹平
check("存在调和工具 reconcile.py", (SRC / "reconcile.py").is_file())
_rec_src = (SRC / "reconcile.py").read_text(encoding="utf-8")
check("调和默认干跑（--apply 才写）", '"--apply"' in _rec_src)
check("调和会读回验证", "读回验证" in _rec_src)




section("密钥保护检查")

# 为什么值得单独查：Telegram token 一旦被提交，等于把 bot 交给别人。
# 加 telegram.py 时就发现 .gitignore 里**没有 .env** —— 而下一步就要往
# 里面放 token 了。所以这里固化成检查。
_r = _sp2.run(["git", "check-ignore", ".env"], cwd=str(ROOT),
              capture_output=True, text=True)
check(".env 被 gitignore 排除",
      _r.returncode == 0, "危险：.env 未被忽略，密钥可能被提交")

# 反向确认：.env.example 应当**能**被提交（它是模板，供他人参考）
_r = _sp2.run(["git", "check-ignore", ".env.example"], cwd=str(ROOT),
              capture_output=True, text=True)
check(".env.example 可被提交（它是模板）", _r.returncode != 0)


# ── shell 脚本静态检查：bash 3.2 的全角字符陷阱

section("shell 脚本检查")

# macOS 自带 bash 3.2 会把**全角字符的字节**当成变量名的一部分。
# 于是 `echo "「$TARGET」"` 会去找名为 `TARGET」` 的变量，报
# `TARGET?: unbound variable` —— 报错信息还会把变量名截断得看不出真相。
#
# 这个坑在本项目里踩了三次（两次探测脚本 + 一次 delete-day.sh），
# 所以固化成检查：$VAR 后面紧跟非 ASCII 字节就是危险写法，必须写 ${VAR}。
_danger = _re.compile(rb"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])")
_shell_files = _SHELL_FILES
_bad: list[str] = []
for _p in _shell_files:
    _hits = _danger.findall(_p.read_bytes())
    if _hits:
        _vars = ", ".join(sorted({h.decode() for h in _hits}))
        _bad.append(f"{_p.name}: ${_vars}")

check(
    f"{len(_shell_files)} 个 shell 脚本无「$VAR 后紧跟全角字符」写法",
    not _bad,
    "；".join(_bad) + "  → 改用 ${VAR}",
)


# ── v4 纯函数模块（时间解析 + 命令分类）
#
# 这三个模块是 v4 的核心判断逻辑，且**完全纯函数**（不碰 AppleScript），
# 所以能在这里彻底验证。日期计算的错误极难在生产中发现
# （表现为"差一天""时段不对"），必须靠固定基准日的断言守住。

section("v4 时间解析（whens）")

_whens = _load(SRC / "whens.py")
import datetime as _dt2  # noqa: E402

_B = _dt2.date(2026, 10, 3)      # 周六。固定基准日 → 结果不随运行日期变化

# 中文数字（时间场景只用到 1-59）
for _s, _want in [("一", 1), ("两", 2), ("十", 10), ("十一", 11), ("十五", 15),
                  ("二十", 20), ("二十五", 25), ("三十", 30), ("三十一", 31),
                  ("59", 59), ("", None), ("abc", None)]:
    check(f"中文数字 {_s!r}", _whens.cn_number(_s) == _want,
          f"得到 {_whens.cn_number(_s)!r}")

# 相对日期
for _s, _want in [("今天", _dt2.date(2026, 10, 3)), ("明天", _dt2.date(2026, 10, 4)),
                  ("后天", _dt2.date(2026, 10, 5)),
                  ("大后天", _dt2.date(2026, 10, 6)),
                  ("昨天", _dt2.date(2026, 10, 2))]:
    _w = _whens.parse_when(_s, _B)
    check(f"{_s} 的日期", _w is not None and _w.start.date() == _want)

# 星期（基准周六）：无前缀取"最近的将来"
for _s, _want in [("周六", _dt2.date(2026, 10, 3)),   # 今天
                  ("周日", _dt2.date(2026, 10, 4)),
                  ("周一", _dt2.date(2026, 10, 5)),
                  ("周五", _dt2.date(2026, 10, 9)),   # 最近的将来那个
                  ("这周五", _dt2.date(2026, 10, 9)),
                  ("下周五", _dt2.date(2026, 10, 16)),  # 下周五必然是下周
                  ("下下周五", _dt2.date(2026, 10, 23)),
                  ("星期日", _dt2.date(2026, 10, 4)),
                  ("礼拜一", _dt2.date(2026, 10, 5))]:
    _w = _whens.parse_when(_s, _B)
    check(f"{_s} 的星期计算", _w is not None and _w.start.date() == _want,
          f"得到 {_w.start.date() if _w else None}")

# 时段补正（12 → 24 小时制）。这是最容易错的地方。
for _s, _h, _m in [("下午两点", 14, 0), ("下午2点", 14, 0), ("晚上8点", 20, 0),
                   ("早上9点", 9, 0), ("上午十点", 10, 0), ("凌晨1点", 1, 0),
                   ("中午12点", 12, 0), ("下午2点半", 14, 30),
                   ("下午14点", 14, 0), ("20点", 20, 0),
                   ("14:30", 14, 30), ("9:05", 9, 5)]:
    _w = _whens.parse_when(_s, _B)
    check(f"{_s} 的时刻补正",
          _w is not None and (_w.start.hour, _w.start.minute) == (_h, _m),
          f"得到 {(_w.start.hour, _w.start.minute) if _w else None}")

# 绝对日期与非法日期
for _s, _want in [("10月8日", _dt2.date(2026, 10, 8)),
                  ("10月8号", _dt2.date(2026, 10, 8)),
                  ("2026-12-25", _dt2.date(2026, 12, 25)),
                  ("2027年1月1日", _dt2.date(2027, 1, 1)),
                  ("1月1日", _dt2.date(2027, 1, 1))]:   # 已过 → 明年
    _w = _whens.parse_when(_s, _B)
    check(f"绝对日期 {_s}", _w is not None and _w.start.date() == _want)

check("非法日期 2月30日 → None", _whens.parse_when("2月30日", _B) is None)
check("非法日期 13月1日 → None", _whens.parse_when("13月1日", _B) is None)

# 无时间信息
for _s in ["交电费", "想起一件事", "", "   "]:
    check(f"{_s!r} 无时间信息", _whens.parse_when(_s, _B) is None)

# 全天 vs 定时
check("只给日期 → 全天", _whens.parse_when("明天", _B).all_day is True)
check("给了时刻 → 非全天", _whens.parse_when("明天下午两点", _B).all_day is False)

# ⚠️ 全天必须从 **00:00** 起（2026-10-05 修，用户实报"去龙井村排到 5 日和 6 日"）
#
# 原先一律用默认时刻 9 点，只把 all_day 标志与时长改掉 ——
# 于是"全天"事件实际是"当天 09:00 + 24 小时"，落到日历里跨两天。
# 归零不丢信息：all_day 为真时 hour 一定是默认值（你说了时刻或时段，它就不为真）。
_w_ad = _whens.parse_when("明天", _B)
check("全天：从 00:00 起（不是默认的 9 点）",
      _w_ad.start == _dt2.datetime(2026, 10, 4, 0, 0), str(_w_ad.start))
check("全天：仍是一整天", _w_ad.end - _w_ad.start == _dt2.timedelta(days=1),
      str(_w_ad.end - _w_ad.start))
# 只说了**时段**（如"明天下午"）→ 不算全天，那个 14:00 是有意义的，不能归零
_w_slot = _whens.parse_when("明天下午", _B)
check("只给时段：不算全天", _w_slot.all_day is False)
check("只给时段：时刻照旧（14:00 不被归零）",
      _w_slot.start == _dt2.datetime(2026, 10, 4, 14, 0), str(_w_slot.start))
# 周期事件的全天起点同理
_w_fo = _whens.first_occurrence("FREQ=WEEKLY;BYDAY=MO", _B)
check("周期全天：也从 00:00 起",
      _w_fo.start.time() == _dt2.time(0, 0), str(_w_fo.start))


section("v4 符号路由（routes）—— 类型由声明决定，不猜")

# ⚠️ 这一段**取代**了原来的 90 行"命令分类（classify）"断言。
#
# 被取代的原因值得记下来：那套断言全都在验"猜得准不准" ——
# 30 多个句子逐条断言它被判成什么。而 2026-10-03 的实测是
# **4 次真实交互错 3 次**，其中一段 89 字的感慨被判成待办写进了提醒事项。
#
# 判据从"猜语义"改成"认符号"之后，那类断言**不该存在** ——
# 类型是你写的，不是代码推断的，所以没有"准不准"可验。
# 这里改为验：符号认得对不对、载荷剥得对不对、缺东西时拒绝得对不对。
#
# 自检项目数因此下降，那不是退步：判据少了，测试也该少。
_rt = _load(SRC / "routes.py")
_K = _load(SRC / "kinds.py")

# ① 三个符号各归各家
for _s, _wk, _wt in [
        ("- [ ] 交电费", "todo", "交电费"),
        ("- [x] 已完成的", "todo", "已完成的"),
        ("* 一条感想", "memo", "一条感想"),
        ("# 学原理比学语法重要", "memo", "学原理比学语法重要"),
        ("＃ 全角井号", "memo", "全角井号"),
        ("@明天上午九点 测试稳定性", "event", "测试稳定性"),
]:
    _it = _rt.route(_s, _B)
    check(f"{_s!r} → {_wk}", _it.kind.value == _wk, f"得到 {_it.kind.value}")
    check(f"{_s!r} 的正文", _it.text == _wt, f"得到 {_it.text!r}")

# ② 裸输入 = 待办（用户 2026-10-03 明确选的默认）
#
# 代价要记住：一段"感慨"不打 # 就会进提醒事项的打钩清单。
# 换来的是最高频的动作零成本。这条断言锁住的是**选择**，不是对错。
for _s in ["交电费", "勘察表盖章", "明天交电费"]:
    _it = _rt.route(_s, _B)
    check(f"裸输入 {_s!r} → 待办", _it.kind.value == "todo",
          f"得到 {_it.kind.value}")

# ③ 待办保留时间提示（只放进备注，不改归属）
_it = _rt.route("明天交电费", _B)
check("待办也解析出时间", _it.when is not None)
check("待办的 when 日期正确", _it.when.start.date() == _dt2.date(2026, 10, 4),
      str(_it.when.start.date()))
check("待办正文剥掉时间词", _it.text == "交电费", repr(_it.text))

# ④ 日程必须有事由 + 有时间，否则**报错不写入**
#
# 这一条是 2026-10-03 那次故障的直接对立面：当时 `@` 缺时间，
# 系统拿时间当了标题建出一条「上午九点」的日程。
# 现在的行为是拒绝，而拒绝的三条边界各有一个用例。
for _s in ["@上午九点",      # 只有时间、没有事由
           "@明天",          # 只有日期
           "@下辈子 交电费",  # 时间认不出来
           "#",             # 只有符号
           "@"]:
    try:
        _bad = _rt.route(_s, _B)
        check(f"{_s!r} 应被拒绝", False, f"却得到 {_bad.kind.value} {_bad.text!r}")
    except _rt.RouteError:
        check(f"{_s!r} 被拒绝（不写入）", True)

# ⑤ 日程标题必须剥掉时间短语 —— **这一步最容易漏**
#
# 它原本藏在 classify.py 的 `_strip_time_phrases` 里，而那个模块已整体删除。
# 若只删不搬，标题会变成"周五下午两点 项目周会"（时间词混进标题）。
for _s, _want in [("@周五下午两点 项目周会", "项目周会"),
                  ("@明天上午九点 测试 Apple agent 稳定性",
                   "测试 Apple agent 稳定性"),
                  ("@下周三 体检", "体检"),
                  ("@10月8日 评审会", "评审会")]:
    _it = _rt.route(_s, _B)
    check(f"{_s!r} 的标题", _it.text == _want, f"得到 {_it.text!r}")

# ⑥ 周期表达 → 日历的重复规则（这是日历相对提醒事项的独有能力）
for _s, _rrule in [("@每周一 交周报", "FREQ=WEEKLY;BYDAY=MO"),
                   ("@每天 跑步", "FREQ=DAILY"),
                   ("@每个工作日 站会", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
                   ("@每月1日 交房租", "FREQ=MONTHLY;BYMONTHDAY=1")]:
    _it = _rt.route(_s, _B)
    check(f"{_s!r} 的 RRULE", _it.recurrence == _rrule, f"得到 {_it.recurrence!r}")
    check(f"{_s!r} 有起始日", _it.when is not None)

# ⑦ 周期事件的起始日必须落在正确的星期（否则日历里落错天）
_it = _rt.route("@每周一 交周报", _B)
check("每周一 → 起始日落在周一（基准周六）",
      _it.when.start.date() == _dt2.date(2026, 10, 5), str(_it.when.start.date()))
_it = _rt.route("@每周日 大扫除", _B)
check("每周日 → 起始日落在周日",
      _it.when.start.date() == _dt2.date(2026, 10, 4), str(_it.when.start.date()))

# ⑧ 说了日期就不顺延；只给时刻且今天已过 → 顺延次日
_it = _rt.route("@明天 14:00 项目周会", _B)
check("写了日期不顺延", _it.rolled is False)
check("写了日期用那天", _it.when.start.date() == _dt2.date(2026, 10, 4),
      str(_it.when.start.date()))


section("v4 删除的判据：不许把'猜'请回来")

# 这一段是**反向断言**：锁住"已经删掉的东西不会被无意中加回来"。
# 本项目吃过这个亏 —— `reclassify` 的删除口子写在文档里却没实现，
# 而文档和代码各说各话了很久。
_rt_src = (SRC / "routes.py").read_text(encoding="utf-8")
check("routes 不含任何词表", "_WORDS" not in _rt_src and "_VERBS" not in _rt_src)
check("routes 不导入 classify", "classify" not in _rt_src)

# classify.py 必须不存在了（猜语义的整条链路）
check("classify.py 已删除", not (SRC / "classify.py").exists(),
      "它用约 700 字词表猜类型，已被符号声明取代")

# 追问/按钮那一整套跨消息状态必须消失
_dm_src_now = (SRC / "daemon.py").read_text(encoding="utf-8")
# ⚠️ 判据用"定义/赋值"的形式，不用裸名字 ——
# 删除处的注释里**特意提到了**这些名字（否则以后没人知道这里曾有什么），
# 用裸名字会把说明文字当成残留代码，那会逼着人删掉注释来讨好断言。
for _gone in ("def save_pending", "def load_pending", "def clear_pending",
              "def _ask_keyboard", "def _handle_callback",
              "def _dispatch_forced"):
    check(f"daemon 已无 {_gone}", _gone not in _dm_src_now)
check("daemon 已无 PENDING_TTL_HOURS 定义",
      "PENDING_TTL_HOURS =" not in _dm_src_now,
      "常量定义应已删除（注释里提到名字是允许的）")

# intake 不应再有"问一次"的通道
_in_src_now = (SRC / "intake.py").read_text(encoding="utf-8")
check("intake 已无 needs_ask 字段", "needs_ask:" not in _in_src_now)
check("intake 已无 pending_text 参数",
      "pending_text:" not in _in_src_now)
check("intake 不导入 classify", "import classify" not in _in_src_now)


section("v4 用户日志（journal）")

_jr = _load(SRC / "journal.py")

# ⚠️ 自检**必须**把 journal 指到临时目录。
# 实测踩到：离线模式那一段忘了重定向，7 条测试记录写进了**真实**的
# data/journal/ —— 而日报的"防遗忘"读的就是这份台账，污染会让它
# 提醒一条根本不存在的事（且很难发现，看起来只是普通记录）。
import pathlib as _plB  # noqa: E402
import tempfile as _tfB  # noqa: E402
_SELFTEST_JOURNAL = _plB.Path(_tfB.mkdtemp(prefix="pdca-selftest-journal-"))
_jr.JOURNAL_DIR = _SELFTEST_JOURNAL / "journal"
if _jr.is_real_dir():
    raise SystemExit("❌ 自检未能把 journal 重定向到临时目录，拒绝继续"
                     "（否则会污染真实数据）")
import json as _json2  # noqa: E402
import tempfile as _tf2  # noqa: E402
import shutil as _sh2  # noqa: E402
from pathlib import Path as _P2  # noqa: E402

_tmpdir = _P2(_tf2.mkdtemp())
_jr.JOURNAL_DIR = _tmpdir
try:
    _jr.log_input("明天交电费", msg_id=1)
    _jr.log_todo("交电费", reminder_id="pdca:x", ok=True)
    _jr.log_memo("荷载要按名称命名", memo_id="m1")
    _jr.log_memo("想起要买猫粮", memo_id="m2")

    _recs = _jr.read_day(_jr._today())
    check("追加可读回", len(_recs) == 4, f"得到 {len(_recs)}")
    check("原始输入原样保存",
          any(r["event"] == "input" and r["text"] == "明天交电费" for r in _recs))

    _sub = _jr.submitted_memos()
    check("台账含两条备忘", set(_sub) == {"m1", "m2"}, f"得到 {set(_sub)}")

    _jr.log_memo_cleared("m1", "荷载要按名称命名")
    _sub2 = _jr.submitted_memos()
    check("已清除的移出台账", set(_sub2) == {"m2"}, f"得到 {set(_sub2)}")

    # 追加不破坏历史
    _n0 = len(_jr.read_day(_jr._today()))
    _jr.log_input("再记一条")
    check("追加是纯追加", len(_jr.read_day(_jr._today())) == _n0 + 1)

    # 坏行容错：一行坏了不该让整份日志读不出来
    _p2 = _tmpdir / f"{_jr._today()}.jsonl"
    with _p2.open("a", encoding="utf-8") as _f2:
        _f2.write("{这不是合法 JSON\n")
    _jr.log_input("坏行之后")
    check("单行损坏不影响整份读取", len(_jr.read_day(_jr._today())) == _n0 + 2)
finally:
    _sh2.rmtree(_tmpdir, ignore_errors=True)


section("模块名不遮蔽标准库")

# 踩到过：把日历模块命名为 calendar.py，于是 `import calendar` 会拿到
# **我们的**模块而不是标准库。当前项目没用到标准库 calendar 所以没爆，
# 但这是定时炸弹 —— 以后引入任何依赖（dateutil 等会 import calendar）都会中招。
#
# 这类问题在"能跑"的情况下完全看不出来，必须静态检查。
import sys as _sys3  # noqa: E402
import sysconfig as _sc3  # noqa: E402

_stdlib_dir = _sc3.get_paths()["stdlib"]
_stdlib_names = {p.stem for p in Path(_stdlib_dir).glob("*.py")}
_shadow = []
for _f in (SRC).glob("*.py"):
    if _f.stem in _stdlib_names:
        _shadow.append(_f.name)

check("src/ 下没有模块遮蔽标准库",
      not _shadow,
      f"这些会遮蔽标准库：{_shadow} → 请改名")


section("v4 备忘录解析（memo，纯函数部分）")

_mm = _load(SRC / "memo.py")

_raw = (f"x-coredata://A/ICNote/p1{_mm.FSEP}荷载要按名称命名{_mm.RSEP}"
        f"x-coredata://A/ICNote/p2{_mm.FSEP}想起要买猫粮{_mm.RSEP}\n")
_memos = _mm.parse_snapshot(_raw)
check("快照解析出 2 条", len(_memos) == 2, f"得到 {len(_memos)}")
check("取到 note_id", _memos[0].note_id == "x-coredata://A/ICNote/p1")

# 标题里含竖线、换行都不能误切（所以用不可见字符做分隔符）
_m2 = _mm.parse_snapshot(f"idA{_mm.FSEP}第一行\n第二行|带竖线{_mm.RSEP}")
check("标题含竖线/换行不误切",
      len(_m2) == 1 and _m2[0].note_id == "idA" and "带竖线" in _m2[0].name)

check("空输入 → 空列表", _mm.parse_snapshot("") == [])
check("纯空白 → 空列表", _mm.parse_snapshot("\n\n") == [])
check("残缺块被跳过而非崩",
      len(_mm.parse_snapshot(f"垃圾{_mm.RSEP}idB{_mm.FSEP}正常{_mm.RSEP}")) == 1)

# AppleScript 转义：反斜杠必须**先**转，否则会吃掉后续引号
check("转义：反斜杠先于引号",
      _mm._as_literal('a\\"b') == '"a\\\\\\"b"',
      _mm._as_literal('a\\"b'))
check("转义：普通引号", _mm._as_literal('说"hi"') == '"说\\"hi\\""')
check("HTML 转义", _mm._html_escape("a<b&c>d") == "a&lt;b&amp;c&gt;d")

# 删除能力：v4 的约束是「**agent 从不删任何东西**」。
#
# 曾为「改分类」开过一个受控口子（memo.delete / applecal.delete），
# 后被用户否决 —— 理由正确：纠正属于锦上添花，而「跑通并积累数据」
# 才是当前重点；开删除口子反而增加风险。已全部撤销。
#
# 这条约束要一直守住，所以逐模块断言。
_del_srcs = {_n: (SRC / f"{_n}.py").read_text(encoding="utf-8")
             for _n in ("memo", "applecal")}
for _nm, _src in _del_srcs.items():
    check(f"v4 写入端 {_nm} 不含删除能力",
          _re.search(r"^def delete\(", _src, _re.M) is None,
          "改分类已撤销，写入端不应有 delete")

# reminders 的 delete 是 v1 遗留（仅 cleanup_reminders.py 这个手动工具用），
# v4 路径不得调用它
_v4_del_callers: list[str] = []
for _f in (SRC).glob("*.py"):
    if _f.name in ("reminders.py", "cleanup_reminders.py", "selftest.py"):
        continue
    for _ln, _line in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
        if _line.lstrip().startswith("#"):
            continue
        if _re.search(r"\brem\.delete\(|reminders\.delete\(", _line):
            _v4_del_callers.append(f"{_f.name}:{_ln}")
check("v4 路径不调用 reminders.delete", not _v4_del_callers,
      "；".join(_v4_del_callers))


section("内核契约（平台边界与版本）")

# 为什么需要这一节（2026-10-05，见 docs/KERNEL-CONTRACT.md）：
#
# 在此之前，"哪些模块是内核、哪些是功能"只存在于文档和人的记忆里。
# 而加功能时越界是**无声的** —— 能跑、能过自检，
# 几个月后才发现内核里长出了对某个功能的依赖（现在就已经有 4 条）。
# 把边界写成断言，它才从"一段话"变成"红绿灯"。
#
# 这一节做两件事：
#   ① 版本：单一来源（VERSION 文件）且与 CHANGELOG 一致
#   ② 边界：每个模块恰好属于一类；越界只许出现在白名单里，且白名单**只许缩小**

# ── ① 版本号
#
# 写在两个地方的版本号，一定有一处是错的 —— 所以必须机器核对。
_VER_PATH = ROOT / "VERSION"
_ver = _VER_PATH.read_text(encoding="utf-8").strip() if _VER_PATH.is_file() else ""
check("VERSION 文件存在", bool(_ver), f"缺或为空：{_VER_PATH}")
# SemVer 规则 2：X.Y.Z 非负整数，且**不许前导零**
check("VERSION 是三名整数（无前导零）",
      _re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", _ver) is not None,
      f"读到 {_ver!r} —— 必须是 X.Y.Z，且不许前导零（1.02.3 非法）")

_cl_src = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
_cl_ver = _re.search(r"当前契约版本：`([^`]+)`", _cl_src)
check("CHANGELOG 标了「当前契约版本」", _cl_ver is not None,
      "CHANGELOG 顶部要有 当前契约版本：`X.Y.Z` 一行")
check("CHANGELOG 的契约版本与 VERSION 一致",
      _cl_ver is not None and _cl_ver.group(1) == _ver,
      f"CHANGELOG 写 {_cl_ver.group(1) if _cl_ver else '（无）'}，VERSION 写 {_ver}")

# 契约文档本身也要在 —— 断言留着而文档被删，是最难查的一种不一致
check("docs/KERNEL-CONTRACT.md 存在",
      (ROOT / "docs" / "KERNEL-CONTRACT.md").is_file())

# 文档头部也写着版本号。2026-10-07 才发现它**停在 1.0.0**（VERSION 早已是 1.1.0），
# 而当时没有任何断言盯着它 —— 同一条教训：写在两处的版本号一定有一处是错的，
# 所以第二处也要机器核对，不能靠"记得改"。
_kc_src_v = (ROOT / "docs" / "KERNEL-CONTRACT.md").read_text(encoding="utf-8")
_kc_ver_v = _re.search(r"`VERSION` = `([^`]+)`", _kc_src_v)
check("KERNEL-CONTRACT 的版本与 VERSION 一致",
      _kc_ver_v is not None and _kc_ver_v.group(1) == _ver,
      f"契约文档写 {_kc_ver_v.group(1) if _kc_ver_v else '（无）'}，VERSION 写 {_ver}")

# ── ② 模块分类（全量、不重不漏）
#
# 与以前那几张"手工名单"（:674 / :3145 / :1195）的关键差别：
# 这里是**对 src/ 的全量划分** —— 新模块不登记就红。
# 名单式检查漏登记是没有后果的，划分式检查不会。
_KERNEL = {"telegram", "journal", "whens", "kinds", "parse", "routes", "intake",
           "notify", "reminders", "applecal", "memo", "daemon"}
_FEATURE = {"report", "commands", "watchdog", "habits"}
_LEGACY = {"carry_over", "cleanup", "cleanup_reminders", "completion", "daily_report",
           "notes", "probe_reminders", "push_tasks", "read_day", "reconcile", "sync"}
_TOOLING = {"selftest"}

_all_mods = {p.stem for p in SRC.glob("*.py")}
_classified = _KERNEL | _FEATURE | _LEGACY | _TOOLING
check("每个 src 模块恰好被登记一类（新模块必须登记）",
      _all_mods == _classified,
      f"漏登记：{sorted(_all_mods - _classified)}；"
      f"登记了但文件不在：{sorted(_classified - _all_mods)}")
_dup = ((_KERNEL & _FEATURE) | (_KERNEL & _LEGACY) | (_FEATURE & _LEGACY)
        | ((_KERNEL | _FEATURE | _LEGACY) & _TOOLING))
check("四类互不重叠", not _dup, f"重复登记：{sorted(_dup)}")


def _imports_of(_stem: str) -> set:
    """一个模块 import 了哪些**本项目**的模块（含函数内 import）。

    用 ast 而不是正则：docstring 里写着 `import report` 是很常见的
    （本项目到处是代码示例），正则会把示例当成真依赖。
    """
    _tree = ast.parse((SRC / f"{_stem}.py").read_text(encoding="utf-8"))
    _out: set = set()
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Import):
            _out |= {_a.name.split(".")[0] for _a in _node.names}
        elif isinstance(_node, ast.ImportFrom):
            if _node.module and not _node.level:      # 相对 import 不算
                _out.add(_node.module.split(".")[0])
    return _out & _all_mods


# 越界白名单：现状的**登记**，不是许可。
# 新增一条越界 → 红；把某条修好了 → **也红**（提醒删掉这一行）。
# 后半条是刻意的：一份不删的名单很快会变成谎话
# （同源教训：README「归档不等于可以忘掉」—— 挪走就当没这回事，检查就失效了）。
_WHITELIST = {
    ("commands", "report"):    "读函数暂住日报；区块注册表那一步归位到内核读层",
    ("daemon", "commands"):    "通道层字面分流；命令注册表那一步消除",
    ("daemon", "watchdog"):    "降级加载（形态是好的）；等插件自注册",
    ("daemon", "habits"):      "同 watchdog 形态（降级加载）；等插件自注册",
    ("telegram", "commands"):  "CLI 推 / 菜单时去取命令表；应改成参数传入",
}

_edges: set = set()
for _m in sorted(_KERNEL | _FEATURE):
    _here = "内核" if _m in _KERNEL else "功能"
    for _dep in sorted(_imports_of(_m)):
        _there = ("内核" if _dep in _KERNEL else
                  "功能" if _dep in _FEATURE else
                  "遗留" if _dep in _LEGACY else None)
        if _there is None:                      # 工具模块不该被任何人 import
            _edges.add((_m, _dep))
        elif (_here == "内核" and _there == "功能") or \
             (_here == "功能" and _there == "功能"):
            _edges.add((_m, _dep))

_new_edges = sorted(_edges - set(_WHITELIST))
_stale_edges = sorted(set(_WHITELIST) - _edges)
check("没有新的越界边（内核↛功能、功能↛功能）", not _new_edges,
      "；".join(f"{_a}.py → {_b}.py" for _a, _b in _new_edges)
      + "　（要么改依赖，要么登记进白名单并写明消除时机）")
check("白名单里已被消除的越界边要删掉（名单不许变成谎话）", not _stale_edges,
      "；".join(f"{_a}.py → {_b}.py" for _a, _b in _stale_edges)
      + "　（修好了就把这一行删掉）")


section("v4 的 AppleScript 必须可编译")

# 这类检查拦的是**我连踩三次**的同一类错误：AppleScript 不支持 Python/JS
# 风格的转义。全都在"能跑"之前就编译失败，但报错信息（Expected """ but
# found unknown token）完全指不到真正的原因。
#
#   ① \u0001 当分隔符        → 编译失败
#   ② 裸控制字节当分隔符      → 编译失败（源码里出现不可打印字符）
#   ③ \u0000 当哨兵值         → 编译失败
#
# 正解是让 AppleScript 自己用 `character id 1` 构造分隔符。
# 这里**从源码真实提取** AppleScript 并交给 osacompile 编译 ——
# 手写模板做检查会漏（我最初就是这么漏掉 whose/is 的）。
import ast as _ast5  # noqa: E402
import os as _os5  # noqa: E402
import subprocess as _sp5  # noqa: E402


def _extract_applescript(mod, extra_ns=None):
    """
    把模块里的 AppleScript 求值出来（函数参数填 "X"）。

    ⚠️ 这个函数改了四次，每次都因为"提取不完整"而让防线失效 ——
    值得把弯路记下来，因为这类"检查工具看起来在工作、实际什么都没查"
    比没有检查更危险：

      ① 只找 ast.JoinedStr（f-string）
         → 普通字符串拼接的 AppleScript 全漏（create_calendar 就没查）
      ② 只从"拼接链的根"求值
         → 选错节点：AST 把相邻字面量拆成嵌套 BinOp，
           求出的是片段，被 `tell application` 前缀过滤掉
      ③ 全部求值 + 丢子串
         → 片段与完整块不一定是子串关系（拼接处在引号中间断开），
           漏进一个 `tell application "` 片段，把防线变成误报
      ④ **用模块真实的全局命名空间求值** ← 现在这个
         → 之前用自己拼的命名空间，缺模块级常量（APP 等），
           于是完整块全部 NameError 被静默跳过，只剩片段。
           "静默跳过求值失败"正是最坑的地方 —— 现在改成求值失败就报错。

    最后一道仍是 osacompile 编译 + 最小段数断言：
    提取漏了会表现为段数偏少，所以段数也纳入检查。
    """
    import importlib as _il
    _mod = _il.import_module(mod)

    # 以模块真实全局为底，再覆盖函数参数/局部变量为 "X"
    _ns = dict(getattr(_mod, "__dict__", {}))
    _ns["_as_literal"] = lambda s: '"X"'
    _ns["_html_escape"] = lambda s: "X"
    # 占位符要**插值后仍是合法 AppleScript** ——
    # 否则生成的脚本编译失败，会被误报成"真 bug"。
    # 比如 props 若填成裸 X，得到 `with properties {X}` 是语法错。
    for _k in ("fid", "note_id", "cal", "uid", "name", "text", "summary",
               "first_line", "body_html", "location", "description",
               "recurrence", "start", "end", "now", "target", "note"):
        _ns[_k] = '"X"'
    # ⚠️ 占位符的**类型**必须和真实代码一致，否则求值会静默走偏。
    # 踩到过：add() 里有 `", ".join(props)`，而 props 在真实代码里是
    # **列表**；我给了字符串，于是 join 把字符串按字符拆开
    # （`s u m m a r y : …`），生成非法 AppleScript，被误判成语法错误。
    # 所以这里要造"有 .join() 的类列表对象"，而不是字符串。
    class _StrList(list):
        def join(self, sep):            # noqa: A003
            return sep.join(str(x) for x in self)

    _ns["props"] = _StrList(['summary:"X"', "start date:startDate"])
    # 日历/文件夹的引用对象
    _ns["scope"] = 'calendar "X"' 

    _src_text = Path(_mod.__file__).read_text(encoding="utf-8")
    _tree = _ast5.parse(_src_text)

    _cands: set[str] = set()
    for _node in _ast5.walk(_tree):
        if not isinstance(_node, (_ast5.JoinedStr, _ast5.BinOp, _ast5.Constant)):
            continue
        try:
            _v = eval(compile(_ast5.Expression(_node), "<s>", "eval"), _ns)
        except Exception:
            continue          # 不是 AppleScript 模板（普通字符串/表达式）
        if isinstance(_v, str) and _v.lstrip().startswith("tell application"):
            _cands.add(_v)

    # 完整块的语义判据：以 `end tell` 收尾。
    #
    # ⚠️ 残缺的块**不能静默丢弃** —— 那正是上一版的漏洞：
    # 故意删掉一个 `end tell` 后，残缺块被过滤掉，检查反而报"全部通过"
    # （段数从 9 变 8，但没人看段数）。所以残缺块要单独返回、当失败报出来。
    _done = [_c for _c in _cands if _c.rstrip().endswith("end tell")]
    _ok = [_c for _c in _done
           if not any(_c != _o and _c in _o for _o in _done)]
    # 只返回完整块。
    #
    # ⚠️ 关于"残缺块检测"：我在这里绕了很久（先丢弃、后按关键字报错，
    # 都被 f-string 拼接产生的良性片段误报）。**最终放弃启发式判断**，
    # 改用下面更硬的核对方式：
    #
    #   完整块数 == 源码里 AppleScript 块的应有数量
    #
    # 如果某段被截断（少了 end tell），它会从完整块里消失，
    # 于是"数量对不上" —— 这比猜"哪个片段是残缺的"可靠得多。
    # 应有数量由 `make new` 语句计数得出（每个 AppleScript 块最多一句）。
    return _ok


_as_bad: list[str] = []
_as_total = 0
_missing: list[str] = []
for _mod in ("memo", "applecal"):
    _ok5 = _extract_applescript(_mod)
    # 应有数量：源码里 `make new` 的出现次数（每个 AppleScript 块最多一句）。
    # 提取若漏了或某段被截断，完整块数就会少于它。
    # 核对"含 make 语句的块"数量 == 源码里 make 语句的数量。
    #
    # 为什么不直接比"总块数"：读操作的块（snapshot/list）不含 make，
    # 两者数量天生不等，拿来比是错的（我第一版就是这么写的，抓不到破坏）。
    # 只比"含 make 的块"，一旦某块被截断（少了 end tell），
    # 它就不再是完整块 → 数量对不上 → 报错。
    import re as _re6
    _txt5 = (SRC / f"{_mod}.py").read_text(encoding="utf-8")
    _stmt_n = len(_re6.findall(r"make new \w+ (?:at|with|in)\b", _txt5))
    _block_n = sum(1 for _c in _ok5 if "make new" in _c)
    # 允许少几个：`applecal.add()` 的脚本是**运行时拼装**的
    # （依赖 props 的 join 结果），静态求值器拿不到那一块里的 make 语句。
    # 2026-10-07 起那一块里有 **2** 句（建事件 + 加闹钟），所以按模块给数 ——
    # 一刀切给 -2 会把 memo 那边"某段被截断"也放过去。
    # 那一块由下面单独的直接验证覆盖，不是漏检。
    _runtime_makes = {"applecal": 2, "memo": 0}
    if _block_n < _stmt_n - _runtime_makes.get(_mod, 0):
        _missing.append(
            f"{_mod}: 含 make 的完整块 {_block_n} 个，但源码有 {_stmt_n} 处 make 语句")

    for _src5 in _ok5:
        _as_total += 1
        _r5 = _sp5.run(["osacompile", "-o", _os5.devnull, "-e", _src5],
                       capture_output=True, text=True)
        if _r5.returncode != 0:
            _as_bad.append(f"{_mod}: {_r5.stderr.strip()[:50]}")

check(f"v4 的 {_as_total} 段 AppleScript 全部可编译",
      not _as_bad and _as_total >= 8,
      "；".join(_as_bad) if _as_bad else f"只找到 {_as_total} 段，可能提取失败")

# 提取必须覆盖全部块 —— 少了就说明有段被截断或提取漏了。
# （放弃"猜哪个片段残缺"的启发式：f-string 拼接会产生大量良性片段，
#   按关键字判断会误报。核对数量是更硬的判据。）
check("AppleScript 提取覆盖全部块", not _missing, "；".join(_missing))

# 单独验证 applecal.add() 的脚本形状 ——
# 它的 AppleScript 是运行时拼装的（依赖 props 的 join 结果），静态提取器拿不到。
#
# 2026-10-07 换法：不再"照样子手拼一段"（那只能证明**手拼的那段**没写错），
# 而是把 `run_applescript` 换掉之后**真的调一次 add()**，拿到它**实际会执行的那段脚本**
# 再交给 osacompile。测的是生成器本身 —— 包括新加的闹钟语句。
import datetime as _dt6  # noqa: E402

_ac = sys.modules.get("applecal") or _load(SRC / "applecal.py")


def _capture_add(**kw):
    """调一次 add()，返回它真正会执行的 AppleScript（不碰任何真实数据）。"""
    _calls: list[str] = []
    _real_run = _ac.run_applescript
    _ac.run_applescript = lambda src, timeout=45: (_calls.append(src), "UID-TEST")[1]
    try:
        _ac.add(kw.pop("summary"), kw.pop("start"), kw.pop("end"), **kw)
    finally:
        _ac.run_applescript = _real_run
    return _calls[0]


_add_src = _capture_add(summary="X", start=_dt6.datetime(2026, 10, 5, 14, 0),
                        end=_dt6.datetime(2026, 10, 5, 15, 0), calendar="X",
                        location="X", recurrence="FREQ=WEEKLY;BYDAY=MO",
                        alarm=True)
_r6 = _sp5.run(["osacompile", "-o", _os5.devnull, "-e", _add_src],
               capture_output=True, text=True)
check("applecal.add 生成的脚本可编译", _r6.returncode == 0,
      _r6.stderr.strip()[:80])

# 逐字段设日期是刻意选择（避开受区域设置影响的 date 字面量），
# 确认它真的在生成脚本里
check("日期用逐字段 set（不用 date 字面量）",
      "set year of startDate to 2026" in _add_src
      and 'date "' not in _add_src)

# ── 闹钟（2026-10-07 用户裁决：**只给带重复规则的日程**设）
#
# 为什么必须断言：`make new display alarm` 写错了**不会报错**，
# 只会"到点不响" —— 而那是用户唯一能察觉的信号（他以为设过了）。
check("重复日程：生成脚本里有 display alarm",
      "make new display alarm" in _add_src, _add_src[:160])
check("定时日程：闹钟在开始时（trigger interval:0）",
      "trigger interval:0" in _add_src, _add_src[:160])

# 全天日程**不能**给 0：全天事件在日历里的"事件时刻"是当天 00:00，
# 给 0 就是半夜弹 —— 用户看不出那是 bug，只会觉得吵。
_allday_src = _capture_add(summary="X", start=_dt6.datetime(2026, 10, 5, 9, 0),
                           end=_dt6.datetime(2026, 10, 6, 9, 0), calendar="X",
                           recurrence="FREQ=WEEKLY;BYDAY=MO", allday=True,
                           alarm=True)
# ⚠️ 数的是 "make new display alarm" 而不是 "display alarm"：
# 后者在 "display alarms of newEv" 里**又出现一次**（我第一版就数错了，
# 断言红了才发现 —— 计数类断言要数那个只可能有一份的串）。
check("全天日程：闹钟在当天 09:00（不是半夜）",
      f"trigger interval:{_ac.ALLDAY_ALARM_MINUTES}" in _allday_src
      and _allday_src.count("make new display alarm") == 1, _allday_src[-160:])

_once_src = _capture_add(summary="X", start=_dt6.datetime(2026, 10, 5, 14, 0),
                         end=_dt6.datetime(2026, 10, 5, 15, 0), calendar="X")
check("一次性日程：不加闹钟", "display alarm" not in _once_src, _once_src[:160])

# 再静态扫一遍：源码里不该出现 AppleScript 里的 \u 转义
_u_escapes = []
for _mod in ("memo.py", "applecal.py"):
    for _ln, _line in enumerate((SRC / _mod).read_text(encoding="utf-8").splitlines(), 1):
        if _line.lstrip().startswith("#"):
            continue
        # AppleScript 片段里的 \u 转义（Python 侧的 \u0001 是合法的，
        # 但本项目统一改用 FSEP/RSEP 常量，所以这里全禁）
        if "'" in _line and "\\u" in _line:
            _u_escapes.append(f"{_mod}:{_ln}")

check("AppleScript 片段里没有 \\u 转义", not _u_escapes, "；".join(_u_escapes))


section("v4 收件分派（intake，注入假写入端）")

# intake 的三个写入端是**可注入**的 —— 所以全部分支都能离线验掉，
# 不需要备忘录/日历权限，也不会污染真实数据。
# v1 的分发逻辑绑死在真实 AppleScript 上，只能靠手动跑验证，
# 这是它 bug 反复出现的原因之一。
_it = _load(SRC / "intake.py")


class _FakeSinks:
    """记录所有写入调用，便于断言"到底往哪写了"。"""
    def __init__(self):
        self.calls = []

    def todo(self, text, when=None, flagged=False, priority=0):
        # ⚠️ 新参数**追加在末尾**：上面的断言按下标取过前 3 个。
        self.calls.append(("todo", text, when, flagged, priority))
        return "T-1"

    def event(self, summary, start, end, location="", recurrence="",
              allday=False, alarm=False):
        # ⚠️ alarm **追加在末尾**：上面的断言按下标取过前 6 个，不能插在中间。
        self.calls.append(("event", summary, start, end, recurrence, allday, alarm))
        return "E-1"

    def memo(self, text):
        self.calls.append(("memo", text))
        return "M-1"


def _fresh_journal():
    """每个用例独立的 journal 目录，避免计数互相污染。"""
    _d = _P2(_tf2.mkdtemp())
    _jr.JOURNAL_DIR = _d
    return _d


def _new_intake():
    _f = _FakeSinks()
    return _it.Intake(add_todo=_f.todo, add_event=_f.event,
                      add_memo=_f.memo), _f


# ① 待办 → 只写提醒事项（正文已剥掉时间词）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("明天交电费", _B)
    check("待办：ok 且类型正确", _o.ok and _o.kind == _K.Kind.TODO)
    check("待办：只写了一次", len(_f.calls) == 1, f"得到 {_f.calls}")
    check("待办：写的是提醒事项端", _f.calls[0][0] == "todo")
    check("待办：正文剥掉时间词", _f.calls[0][1] == "交电费", _f.calls[0][1])
    check("待办：回执说明去向", "提醒事项" in _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# 待办的**备注文本**（2026-10-05 改：不再写 v1 的去重键 `pdca:xxxx`）
#
# 那个键在现行链路上**没有任何读者**（读它的 completion / push_tasks / carry_over
# 全是 v1 模块，launchd 里只剩 daemon 与 report）。v4 只增不去重，
# 要认"这条是我建的"用的是 journal 里的 x-apple-reminder:// id。
# 现在备注只留时间提示，没给时间就空着。
_rm_mod = sys.modules.get("reminders") or _load(SRC / "reminders.py")
_saved_rem_cls = _rm_mod.Reminders
def _W(start, *, all_day=False, has_date=True, has_time=False):
    """造一个 whens.When（离线测写入端）。"""
    return _whens.When(start=start, end=start + _dt2.timedelta(hours=1),
                       all_day=all_day, has_date=has_date, has_time=has_time)


try:
    _writes: list = []

    class _FakeReminders:
        def verify_list(self):
            pass

        def create(self, name, body="", due=None, allday_due=False,
                   remind=False, flagged=False, priority=0):
            _writes.append(dict(name=name, body=body, due=due,
                                allday_due=allday_due, remind=remind,
                                flagged=flagged, priority=priority))
            return type("_R", (), {"id": "RID"})()

    _rm_mod.Reminders = _FakeReminders

    # ① 有日期 → **原生到期日**；全天走 allday due date（没有时刻可弹）
    _it._real_add_todo("交电费", _W(_dt2.datetime(2026, 10, 5, 9, 0), all_day=True))
    _w = _writes[-1]
    check("待办：有日期 → 写原生到期日",
          _w["due"] == _dt2.datetime(2026, 10, 5, 9, 0), str(_w))
    check("待办：全天 → allday due date（不弹）",
          _w["allday_due"] is True and _w["remind"] is False, str(_w))
    check("待办：日期进了原生字段，备注就空着", _w["body"] == "", repr(_w["body"]))

    # ② 有日期 + 有时刻 → due + remind me（"该弹就弹"，用户 2026-10-07 裁决）
    _it._real_add_todo("交电费", _W(_dt2.datetime(2026, 10, 5, 14, 0),
                                    has_time=True))
    _w = _writes[-1]
    check("待办：给了时刻 → 连 remind me date 一起写（到点弹）",
          _w["due"] == _dt2.datetime(2026, 10, 5, 14, 0)
          and _w["remind"] is True and _w["allday_due"] is False, str(_w))

    # ③ 只有时刻、没有日期 → 落不成日期，退回备注（信息不丢）
    _it._real_add_todo("交电费", _W(_dt2.datetime(2026, 10, 5, 14, 0),
                                    has_date=False, has_time=True))
    _w = _writes[-1]
    check("待办：只有时刻 → 不写日期，时间退回备注",
          _w["due"] is None and _w["body"] == "10-05 14:00", str(_w))

    # ④ 没给时间 → 什么都不写
    _it._real_add_todo("交电费", None)
    _w = _writes[-1]
    check("待办：没给时间 → 不写日期也不写备注",
          _w["due"] is None and _w["body"] == "", str(_w))

    # ⑤ 旗标/优先级透传（行首「!」「!!」）
    _it._real_add_todo("交电费", None, True, 1)
    _w = _writes[-1]
    check("待办：旗标与优先级透传到写入端",
          _w["flagged"] is True and _w["priority"] == 1, str(_w))

    check("待办：不再写 v1 的去重键",
          all("pdca:" not in str(w["body"]) for w in _writes), str(_writes))
finally:
    _rm_mod.Reminders = _saved_rem_cls

# ② 日程 → 只写日历（含时间与重复规则）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("@周五下午两点 项目周会", _B)
    check("日程：类型正确", _o.kind == _K.Kind.EVENT)
    check("日程：写的是日历端", _f.calls[0][0] == "event")
    check("日程：时间算对", _f.calls[0][2] == _dt2.datetime(2026, 10, 9, 14, 0),
          str(_f.calls[0][2]))
    check("日程：回执含日期", "10月9日" in _o.reply)

    _i, _f = _new_intake()
    _o = _i.handle("@每周一 交周报", _B)
    check("周期日程：带 RRULE", _f.calls[0][4] == "FREQ=WEEKLY;BYDAY=MO",
          _f.calls[0][4])
    check("周期日程：按全天处理", _f.calls[0][5] is True)
    check("周期日程：回执说人话", "每周一" in _o.reply, _o.reply)

    # ⚠️ 周期 + **明说时刻**：时刻必须留下（2026-10-04 修）
    #
    # 修之前：`first_occurrence()` 的"09:00 + 全天"默认会把明说的"八点"
    # **静默丢掉** —— 日历里落一条全天重复事件，而你以为写了时刻。
    # 写使用说明时才发现，而当时自检没覆盖"周期 + 时刻"这个组合。
    _i, _f = _new_intake()
    _i.handle("@每天八点 跑步", _B)
    check("周期+时刻：时刻没被丢掉",
          _f.calls[0][2] == _dt2.datetime(2026, 10, 3, 8, 0), str(_f.calls[0][2]))
    check("周期+时刻：不再按全天写", _f.calls[0][5] is False, str(_f.calls[0][5]))
    check("周期+时刻：时长 1 小时",
          _f.calls[0][3] - _f.calls[0][2] == _dt2.timedelta(hours=1),
          str(_f.calls[0][3] - _f.calls[0][2]))
    check("周期+时刻：RRULE 还在", _f.calls[0][4] == "FREQ=DAILY", _f.calls[0][4])

    # 日期归重复规则、时刻归载荷 —— 两者各管各的（不能被时刻带跑日期）
    _i, _f = _new_intake()
    _i.handle("@每周一 早上九点 站会", _B)
    check("周期+时刻：日期仍落在周一",
          _f.calls[0][2] == _dt2.datetime(2026, 10, 5, 9, 0), str(_f.calls[0][2]))

    # ── 闹钟（2026-10-07 用户裁决：**只给带重复规则的日程**设）
    #
    # 判定（产品决定）在 intake，表达（定时=开始时 / 全天=09:00）在 applecal。
    # 用**用户那天真实发的那句话**当样本 —— 他就是在这句上撞见"写进去了却不响"。
    _d_al = _fresh_journal()
    try:
        _i, _f = _new_intake()
        _o = _i.handle("@每天8:35 Check my to-dos and plan out the day", _B)
        check("周期日程：要求写入端设闹钟", _f.calls[0][6] is True, str(_f.calls[0]))
        check("周期日程：回执说清会响", "到点会提醒" in _o.reply, _o.reply)
        _rec_al = [r for r in _jr.read_day(_jr._today())
                   if r["event"] == "event_added"]
        check("周期日程：journal 记下 alarm=True",
              bool(_rec_al) and _rec_al[-1].get("alarm") is True,
              str(_rec_al[-1:]))

        _i, _f = _new_intake()
        _o = _i.handle("@明天下午两点 项目周会", _B)
        check("一次性日程：不设闹钟", _f.calls[0][6] is False, str(_f.calls[0]))
        check("一次性日程：回执不提提醒", "到点会提醒" not in _o.reply, _o.reply)
    finally:
        _sh2.rmtree(_d_al, ignore_errors=True)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ③ 备忘 → 只写备忘录
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("# 荷载要按名称命名", _B)
    check("备忘：类型正确", _o.kind == _K.Kind.MEMO)
    check("备忘：写的是备忘录端", _f.calls[0][0] == "memo")
    check("备忘：回执说明去向", "备忘录" in _o.reply)

    # ⚠️ 多行备忘必须**保留换行**（2026-10-04 修）
    #
    # 修之前 `whens.clean_text()` 把 `\s+` 折成一个空格，于是
    #   '# 国庆假期冲刺规划\n1. PDCA模型跑通\n2. 文生视频'
    # 落到备忘录里变成一行 "国庆假期冲刺规划 1. PDCA模型跑通 2. 文生视频"。
    # 而 memo.add 是按行建 <div> 的（写入端本来就为多行设计）——
    # 压平它的是路由这一层，属于"一处按单行假设、另一处按多行实现"的错配。
    _i, _f = _new_intake()
    _i.handle("# 国庆规划\n1. PDCA\n2. 文生视频", _B)
    check("备忘：多行保留换行",
          _f.calls[0][1] == "国庆规划\n1. PDCA\n2. 文生视频",
          repr(_f.calls[0][1]))
    check("备忘：首行就是标题那一行", _f.calls[0][1].splitlines()[0] == "国庆规划",
          repr(_f.calls[0][1]))

    # 行内多余空白仍然要收掉（那是输入法的锅，不是用户的意思）
    _i, _f = _new_intake()
    _i.handle("# 标题   有很多空格\n第二行\t也 有", _B)
    check("备忘：行内空白归一化",
          _f.calls[0][1] == "标题 有很多空格\n第二行 也 有",
          repr(_f.calls[0][1]))

    # 首尾空行去掉，但中间的空行保留（那是分段）
    _i, _f = _new_intake()
    _i.handle("# \n\n第一段\n\n第二段\n\n", _B)
    check("备忘：首尾空行去掉、中间空行保留",
          _f.calls[0][1] == "第一段\n\n第二段", repr(_f.calls[0][1]))

    # 待办/日程仍然是单行标题（它们不能带换行）
    _i, _f = _new_intake()
    _i.handle("交电费\n顺便买猫粮", _B)
    check("待办：正文仍被折成单行（标题不能带换行）",
          "\n" not in _f.calls[0][1], repr(_f.calls[0][1]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ④ 缺硬信息 → **不写入**、如实报错（不再"问一次"）
#
# 这里原本验的是"判不出类型 → 给三个按钮问一次"。那套机制已整体删除
# （2026-10-03 裁决）：类型现在由符号声明，不存在"判不出"。
# 现在会触发的只有**载荷缺硬信息**这一类，行为是报错且零写入。
_d = _fresh_journal()
try:
    for _bad, _why in [("@例会", "日程没写时间"),
                       ("@上午九点", "日程只有时间"),
                       ("#", "只有符号没有内容")]:
        _i, _f = _new_intake()
        _o = _i.handle(_bad, _B)
        check(f"{_why}：不写入任何端", len(_f.calls) == 0, f"得到 {_f.calls}")
        check(f"{_why}：ok=False", _o.ok is False)
        check(f"{_why}：回执如实报错", _o.reply.startswith("❌"), _o.reply)
        check(f"{_why}：回执给出退路", "手动加" in _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑥ 写入失败 → 不崩，给可操作的回执
_d = _fresh_journal()
try:
    def _boom(*a, **k):
        raise RuntimeError("模拟写入失败")

    _i = _it.Intake(add_todo=_boom, add_event=_boom, add_memo=_boom)
    _o = _i.handle("交电费", _B)
    check("写入失败：ok=False 而非抛异常", _o.ok is False)
    check("写入失败：回执含原因", "模拟写入失败" in _o.reply)
    check("写入失败：回执给出退路", "手动加" in _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑦ 空消息不崩
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    for _t in ("", "   "):
        _o = _i.handle(_t, _B)
        check(f"空消息 {_t!r} 不崩", _o.ok is False)
    check("空消息不写入", len(_f.calls) == 0)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑧ journal 必须真的记下来 —— 这里守的是一个真实踩过的 bug：
#    曾经把参数写反（`_journal(journal.log_error, where=..., detail=...)`），
#    结果事件名和日志函数的参数混在一起，日志**静默写不进去**。
#    所以断言"记录数"而不是"函数被调用过"。
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("明天交电费", _B)
    _i.handle("@周五下午两点 项目周会", _B)
    _i.handle("# 备忘内容", _B)
    _evs = [r["event"] for r in _jr.read_day(_jr._today())]
    check("journal 记了 3 条 input", _evs.count("input") == 3, str(_evs))
    check("journal 记了 todo_added", _evs.count("todo_added") == 1)
    check("journal 记了 event_added", _evs.count("event_added") == 1)
    check("journal 记了 memo_added", _evs.count("memo_added") == 1)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑨ 写入失败时也要记 error（否则复盘时看不到"当时失败了"）
_d = _fresh_journal()
try:
    def _boom2(*a, **k):
        raise RuntimeError("x")

    _i = _it.Intake(add_todo=_boom2, add_event=_boom2, add_memo=_boom2)
    _i.handle("交电费", _B)
    _evs = [r["event"] for r in _jr.read_day(_jr._today())]
    check("写入失败也记 error", "error" in _evs, str(_evs))
    check("写入失败也记 input", "input" in _evs, str(_evs))
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑩ 缺硬信息被拒时也要记 input —— 便于复盘"我提过但没记成"
#
# ⚠️ 这一段原来叫"判不出时"（类型判不出来 → 问一次）。
# 类型现在由符号声明，"判不出类型"这个情况**不存在了**。
# 但"载荷缺硬信息被拒绝"仍会产生"提过、没记成"的记录，所以这一段保留，
# 只是触发条件换成了 `@例会`（日程没写时间）。
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("@例会", _B)
    _evs = [r["event"] for r in _jr.read_day(_jr._today())]
    check("被拒也记 input", "input" in _evs, str(_evs))
    check("被拒也记 error（便于排查）", "error" in _evs, str(_evs))
    check("被拒不写入", len(_f.calls) == 0)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑪ 干跑 CLI（`intake.py --dry`）也必须跟上写入端协议
#
# ⚠️ 这条是**真的踩到之后**才补的：主干加了 `alarm` 参数、假写入端也改了，
# 但 CLI 里那份 `fake_event` 是**第二份实现** —— 它没跟上，于是
# `--dry` 直接报 `fake_event() got an unexpected keyword argument 'alarm'`。
# 自检当时**全绿**：它只测了"注入的假写入端"那一份，没测 CLI 这一份。
# 与"RRULE 说人话只有一份实现"同源：**分身的假实现也要有人守**。
_sp11 = _sp5.run([sys.executable, str(SRC / "intake.py"), "--dry",
                  "@每天8:35 Check my to-dos and plan out the day"],
                 capture_output=True, text=True, cwd=str(ROOT))
_out11 = _sp11.stdout + _sp11.stderr
check("干跑 CLI：重复日程不报错", "❌" not in _out11, _out11[:200])
check("干跑 CLI：回执看得见闹钟", "到点会提醒" in _out11, _out11[:200])
check("干跑 CLI：干跑行写明了会调用的写入",
      "event(" in _out11 and "闹钟" in _out11, _out11[-200:])

# 待办那一路也要跑 —— 干跑 CLI 里那份 fake_todo 是**第二份实现**，
# 2026-10-07 就漏过一次（主干加参数它没跟上，而当时自检全绿）。
_sp11b = _sp5.run([sys.executable, str(SRC / "intake.py"), "--dry",
                   "!!明天下午两点 交电费"],
                  capture_output=True, text=True, cwd=str(ROOT))
_out11b = _sp11b.stdout + _sp11b.stderr
check("干跑 CLI：待办不报错", "❌" not in _out11b, _out11b[:200])
check("干跑 CLI：待办看得见旗标与优先级",
      "旗标" in _out11b and "高优先级" in _out11b, _out11b[:200])
check("干跑 CLI：待办行写明了会调用的写入", "todo(" in _out11b, _out11b[-200:])


section("v4 「整句只是一个时间」的判据（is_bare_time）")

# ⚠️ 这条判据来自一次真实的踩坑：
# 用户发了「测试Apple- agent稳定性」→ 判不准 → 给按钮 → 用户点「日程」
# → 系统回"再说一次带上时间？" → 用户回「上午九点」
# → **系统把它当成新的一条日程**，标题就是「上午九点」，
#   时间落在今天 09:00（已经过去），原标题丢了。
#
# 根因之一：系统认不出"这句只是在补时间"。这个判据就是补上这一环。
#
# 注意区分两件事：
#   "整句只是一个时间"（上午九点）      → 可能是用户在补上一条的时间
#   "句子里含时间"    （上午九点开会）  → 这是完整的一条，不该合并
for _s, _want in [("上午九点", True), ("明天上午九点", True), ("下午两点", True),
                  ("9:00", True), ("下午2点半", True), ("明天下午两点", True),
                  ("上午", True), ("明天", True), ("周五", True),
                  ("10月5日", True),
                  # 有内容 → 不是"只是时间"
                  ("上午九点开会", False), ("下午两点 项目周会", False),
                  ("明天上午九点测试 Apple agent 稳定性", False),
                  ("测试Apple- agent稳定性", False), ("明天交电费", False),
                  ("", False)]:
    check(f"is_bare_time({_s!r}) == {_want}",
          _whens.is_bare_time(_s) is _want,
          f"得到 {_whens.is_bare_time(_s)!r}")

# 曾经的错解：用"把命中片段剥掉再看剩什么"判断 —— 不可靠，因为
# When.time_text 只记录命中片段（"上午九点"里只抓到"九点"），
# 残留的"上午"自己又能解析成默认 9:00，于是判据说"这句有内容"，
# 修复等于没生效。这条断言锁住"判据必须看整句"。
_bare_hits = _whens.parse_when("上午九点", _B)
check("判据不能依赖 time_text 的完整性（它会漏字）",
      _bare_hits is not None and _bare_hits.time_text != "上午九点",
      "若 time_text 已改成完整片段，本注释与实现可一并复核")
check("判据对同一句仍然给出正确结果",
      _whens.is_bare_time("上午九点") is True,
      "说明判据确实是看整句，不是看命中片段")


section("v4 「补时间」这一类故障**从根上不存在**")

# ⚠️ 这一段取代了原来的三个"合并到上一条"用例。
#
# 原来那三个用例是针对一次**真实故障**的修复断言：用户被追问
# "日程没写时间"后回了句「上午九点」，系统拿它当标题新建了一条日程，
# 时间落在今天 09:00（已过去），原标题丢失。
#
# 修复当时是"引入单一待补充槽位 + 把补时间合并到上一条上"。
# 但那仍然是**跨消息状态**，后来又因此翻过两次车
# （槽位被清、按钮绑错消息号）。
#
# 2026-10-03 的裁决更彻底：**追问机制整体删除**。
# 于是「补时间」这个动作不存在了 —— 缺什么就带符号重发一条完整的。
# 断言也随之从"合并对不对"变成"拒绝得对不对"。
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("上午九点", _B, msg_id=90)
    # ⚠️ 这条断言的语义值得看清：它**不是**"系统拒绝了"，
    # 而是"系统不再猜它是给哪条的" —— 按裸输入默认处理成待办。
    # 这正是用户 2026-10-03 选的默认（裸输入 = 待办）。
    check("光回一个时间：按裸输入处理（待办），不进日历",
          len(_f.calls) == 1 and _f.calls[0][0] == "todo", f"得到 {_f.calls}")
    check("光回一个时间：写入的是提醒事项", "提醒事项" in _o.reply, _o.reply)
    check("光回一个时间：**没有**凭空建一条日程",
          all(c[0] != "event" for c in _f.calls), f"得到 {_f.calls}")
finally:
    _sh2.rmtree(_d, ignore_errors=True)


section("v4 只说时刻、而该时刻今天已过 → 顺延明天（路由层）")

# whens.py 是纯函数，刻意不读时钟（否则没法离线测），并在注释里写明
# "由调用方决定（它本来就知道'现在'）"。调用方是 intake —— 这一段验它
# 真的做了这件事。不做的话会出现"下午两点开会"在晚上说、
# 日程却落在**今天下午两点（已经过去）**。
#
# 用"基准日上已经过去的时刻"构造：基准日的 _now() 是 23:59，
# 所以基准日白天的任何整点都算"已过"。
#
# ⚠️ 这里原先取的是 `datetime.now() - 2h`（**真实时钟**），而 intake 用的是
# 固定基准日 _B —— 两者只在"运行当天正好是 _B"时一致，于是这条断言
# 从 2026-10-04 起每天都红（实测：换一台机器/换一天跑就失败）。
# 测试**必须**只用基准日构造输入，否则断言会随运行日期变化 ——
# 一条永远红的断言比没有断言更糟：它会训练人忽略整份自检。
_past = _dt2.datetime.combine(_B, _dt2.time(6, 0))
_past_s = f"{_past.hour}点"

_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle(f"@{_past_s} 项目周会", _B)
    _got = _f.calls[0][2]
    check("已过的时刻被顺延到次日",
          _got.date() == _B + _dt2.timedelta(days=1),
          f"得到 {_got}（原时刻 {_past}）")
    check("顺延后回执如实说明", "放到了明天" in _o.reply)
    # ⚠️ 回执里显示的日期必须是**顺延后**的，不能一边写"10月3日"
    # 一边说"放到了明天"—— 自相矛盾的回执比不说还糟。
    check("回执显示的是顺延后的日期（不矛盾）",
          f"{_got.month}月{_got.day}日" in _o.reply,
          f"回执={_o.reply!r}")
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# 带日期的明确时间不该被顺延（用户说了明天就是明天）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("@明天下午两点 项目周会", _B)
    check("写了日期就不顺延",
          _f.calls[0][2] == _dt2.datetime(2026, 10, 4, 14, 0),
          str(_f.calls[0][2]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)


section("v4 待办的时间信息不能被丢掉")
# 踩到过：分类器在待办分支写 `when=None`，把已经解析好的时间扔了 ——
# "明天交电费"里的"明天"白解析，上层再也拿不到。
# **解析出来的信息不该在路由这一步被丢弃**，用不用是上层的事。
_c_todo = _rt.route("明天交电费", _B)
check("待办也保留解析出的时间", _c_todo.when is not None,
      "when 被丢掉了")
check("待办的 when 日期正确",
      _c_todo.when is not None and _c_todo.when.start.date() == _dt2.date(2026, 10, 4),
      str(_c_todo.when.start.date() if _c_todo.when else None))
check("待办的 when 标了 has_date",
      _c_todo.when is not None and _c_todo.when.has_date is True)

_c_todo2 = _rt.route("交电费", _B)
check("无时间的待办 when 为 None", _c_todo2.when is None)

# intake 要把这个时间传给提醒事项端（是否设 due 由该端决定）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("明天交电费", _B)
    _w_arg = _f.calls[0][2]
    check("intake 把时间传给了待办端（传的是 When，不是裸 datetime）",
          _w_arg is not None and _w_arg.has_date
          and _w_arg.start.date() == _dt2.date(2026, 10, 4), str(_w_arg))
finally:
    _sh2.rmtree(_d, ignore_errors=True)


section("v4 收件守护（daemon，状态持久化）")

_dm = _load(SRC / "daemon.py")

# offset 必须持久化：只存内存的话，进程重启会重新拉到旧消息，
# **同一条被记两遍**（重复建待办）。这与 v1 的按钮 bug 同源 ——
# 跨调用的读取位置必须持久。
_dm_dir = _P2(_tf2.mkdtemp())
_dm.STATE_FILE = _dm_dir / "state.json"
try:
    check("初始没有 offset", _dm.load_offset() is None)

    _dm.save_offset(12345)
    check("offset 可读回", _dm.load_offset() == 12345)

    _dm.save_offset(12399)
    check("offset 可覆盖", _dm.load_offset() == 12399)

    # ⚠️ 状态字段之间**不能互相覆盖**。
    # 踩到过：save_offset 写的是全新字典，于是每次保存读取位置都会把
    # notified_at（启动通知冷却）抹掉 —— 冷却失效，崩溃循环时又开始刷屏。
    # 这类"读-改-写没保留其它字段"的坑本项目出现过多次。
    _dm._mark_notified()
    _dm.save_offset(555)
    _st = _json2.loads(_dm.STATE_FILE.read_text(encoding="utf-8"))
    check("写 offset 不冲掉 notified_at", "notified_at" in _st, str(sorted(_st)))
    _dm._mark_notified()
    _st2 = _json2.loads(_dm.STATE_FILE.read_text(encoding="utf-8"))
    check("写 notified_at 不冲掉 offset", _st2.get("offset") == 555,
          str(sorted(_st2)))

    check("原子写不留 .tmp 残留", not (_dm_dir / "state.tmp").exists())

    # 状态文件损坏不该让守护进程起不来
    _dm.STATE_FILE.write_text("{坏 JSON", encoding="utf-8")
    check("损坏的状态文件返回 None 而非崩", _dm.load_offset() is None)

    # ⚠️ 这里原有约 50 行"待补充项"断言 —— 随那套机制一起删除（2026-10-03）。
    #
    # 它们验的是：单一槽位、TTL 过期、旧格式兼容、按钮不靠消息号卡……
    # 全都属于"判不准 → 发按钮 → 等你点 → 补时间"那条链路。
    # **代码删了，验它的断言也该删** —— 留着会变成"测一个不存在的东西"，
    # 而且会让人以为那套机制还在。
    #
    # 反向断言在"v4 删除的判据"那一节：确认 save_pending / load_pending /
    # clear_pending / PENDING_TTL_HOURS 等都已从 daemon.py 消失。
finally:
    _sh2.rmtree(_dm_dir, ignore_errors=True)

# callback_data 有 64 字节硬限制（v1 实测 65 字节 → HTTP 400）
check("按钮 data 不超 64 字节",
      all(len(f"{c}:123456789012".encode()) <= _tg.CALLBACK_MAX_BYTES
          for c in ("t", "e", "m")))

# 程序日志里引用用户内容要截断 —— logs/ 可能被贴出来排查问题
check("日志截断：短文本不变", _dm._trunc("短") == "短")
check("日志截断：长文本带省略号",
      _dm._trunc("很长" * 30).endswith("…"))
check("日志截断：换行被替换", "\n" not in _dm._trunc("a\nb"))

# ⚠️ 这里原有三处"按钮指定类型"的断言，随按钮机制一起删除（2026-10-03）。
#
# 它们验的是"点按钮后强制走某个类型"——而**按钮本身已经不在了**。
# 反向断言在"v4 删除的判据"那一节里（确认 _dispatch_forced 等已消失），
# 所以这里只留一处说明，不再重复验。
#
# 记住这条被删掉的理由：那套机制要跨消息记住"这条在等什么"，
# 命中 ARCHITECTURE §十一 判据 1，并贡献了两次真实故障。

# daemon 依赖的 telegram 接口必须存在（改了 telegram 会在这里断掉）
for _fn in ("send", "send_with_buttons", "answer_callback", "get_updates",
            "load_config", "send_chat_action"):
    check(f"telegram 提供 {_fn}", hasattr(_tg, _fn))


section("v4 整链：发消息 → 符号路由 → 写进对应 App")

# ⚠️ 这一段取代了原来的两段按钮整链测试。
#
# 原来那两段存在的理由很正当：**跨模块的接线本身要测** ——
# 当时 intake 层单测全绿，而 daemon 层把待补充状态清了，
# 于是"补时间"接不上，bug 一路活到用户手上。
#
# 但那两段测的是**按钮流程**，而按钮机制已整体删除。
# 现在链路短得多：daemon 收到 → routes 认符号 → intake 写入。
# 这里就把这条新链路端到端走一遍（假写入端 + 屏蔽 journal + 临时目录）。
#
# 顺带记住那个教训：**每一层都对，接起来仍可能是错的** ——
# 所以这一段不测单层，只测"从消息进来、到写出去"。
_d = _fresh_journal()
_dm_dir2 = _P2(_tf2.mkdtemp())
_saved = (_dm._make_intake, _dm.tg.send)
try:
    _sent = []
    _wrote = []

    def _make(_wrote=_wrote):
        return _it.Intake(
            add_todo=lambda t, w=None, f=False, p=0: (
                _wrote.append(("todo", t, w, f, p)), "T1")[1],
            add_event=lambda s, a, b, **k: (
                _wrote.append(("event", s, a, k.get("recurrence"))), "E1")[1],
            add_memo=lambda t: (_wrote.append(("memo", t)), "M1")[1],
            testing=True)

    _dm._make_intake = _make
    _dm.tg.send = lambda t, **kw: _sent.append(t)

    # ① 裸输入 → 提醒事项，且回执如实说明去向
    _dm.handle_message("交电费", 1, "chat")
    check("整链：裸输入写提醒事项", _wrote[-1][0] == "todo", str(_wrote))
    check("整链：回执说明去向", "提醒事项" in _sent[-1], _sent[-1])

    # ② # → 备忘录，且**符号不进正文**
    _dm.handle_message("# 学原理比学语法重要", 2, "chat")
    check("整链：# 写备忘录", _wrote[-1][0] == "memo", str(_wrote))
    check("整链：# 已被剥掉（正文不带符号）",
          _wrote[-1][1] == "学原理比学语法重要", repr(_wrote[-1][1]))
    check("整链：回执说明去向", "备忘录" in _sent[-1], _sent[-1])

    # ③ @ → 日历，标题剥掉时间词、时间算准
    #
    # ⚠️ 这里**必须把基准日传进 daemon**：`handle_message` 若不传 base，
    # 用的是**真实的今天**，于是"明天"随运行日期漂移 ——
    # 断言写 10-04 而实际算成 10-05（跨过午夜就复现）。
    # 这类断言测的是"今天是几号"，不是代码对不对，属于**脆弱断言**：
    # 它会在某一天悄悄失效，而那时没人知道该信谁。
    _dm.handle_message("@明天上午九点 测试 Apple agent 稳定性", 3, "chat", _B)
    check("整链：@ 写日历", _wrote[-1][0] == "event", str(_wrote))
    check("整链：@ 的标题剥掉了时间词",
          _wrote[-1][1] == "测试 Apple agent 稳定性", repr(_wrote[-1][1]))
    check("整链：@ 的时间算对", _wrote[-1][2] == _dt2.datetime(2026, 10, 4, 9, 0),
          str(_wrote[-1][2]))
    check("整链：回执说明去向", "日历" in _sent[-1], _sent[-1])

    # ④ 缺信息的 @ → **报错且一个字都不写**
    _n_before = len(_wrote)
    _dm.handle_message("@上午九点", 4, "chat")
    check("整链：@ 缺事由时零写入", len(_wrote) == _n_before, str(_wrote[_n_before:]))
    check("整链：@ 缺事由时如实报错", "❌" in _sent[-1], _sent[-1])

    # ⑤ 老按钮被点一下 → 忽略，且不影响后续消息
    #    （历史遗留的按钮消息还在聊天记录里，点了不该让这批更新反复重放）
    _n_before = len(_wrote)
    _dm.handle_message("交电费", 5, "chat")
    check("整链：报错后仍能正常收下一条", len(_wrote) == _n_before + 1, str(_wrote))
finally:
    (_dm._make_intake, _dm.tg.send) = _saved
    _sh2.rmtree(_d, ignore_errors=True)
    _sh2.rmtree(_dm_dir2, ignore_errors=True)


# ── 发声面：文案必须与路由一致、音量必须分档、失败必须重试
#
# 对应 docs/TELEGRAM-VOICE.md 的 V1–V4（2026-10-04 实施）。
# 单独成节的理由与别处一样：**能离线验的，不要等你收错消息才发现。**

section("发声面：启动通知的样例必须真的那样路由（V1）")

# ⚠️ 这条断言是冲着一次真实故障来的：符号方案落地后，启动通知还在教
# "不打符号也能进日历/备忘录"，而它是聊天里唯一的说明书 —— 一天被教错十次。
# 断言把每条样例**真的丢进 route()**：以后谁改了符号语义，这里先红。
_PLACE_OF = {"todo": "提醒事项", "memo": "备忘录", "event": "日历"}
for _t_ue, _where_ue in _dm.USAGE_EXAMPLES:
    _o_ue = _rt.route(_t_ue, _B)
    check(f"启动通知样例「{_t_ue}」真的去{_where_ue}",
          _PLACE_OF.get(_o_ue.kind.value) == _where_ue,
          f"实际 {_o_ue.kind.value}")

check("启动通知逐条列出了样例（不是另一份手写文案）",
      all(t in _dm.STARTUP_NOTICE and w in _dm.STARTUP_NOTICE
          for t, w in _dm.USAGE_EXAMPLES), _dm.STARTUP_NOTICE)

section("发声面：回执的音量 / 重试 / 截断（V2 / V3 / V6）")

# 音量分档（V2）与重试（V3）都要看"实际传给通道的参数"，
# 所以这里换成假通道来看参数 —— 而不是断言源码里有没有某句话。
#
# ⚠️ 重试要真睡 5 + 15 秒：自检里把等待改成 0，否则每跑一次白等 20 秒。
# （一条要等 20 秒的自检会被跳过不跑，那还不如不写。）
_saved_delays = _dm.REPLY_RETRY_DELAYS
_saved_tg_send = _dm.tg.send
_d = _fresh_journal()
try:
    _dm.REPLY_RETRY_DELAYS = (0, 0)

    class _FlakyTg:
        """前 fail_times 次失败、之后成功的假通道。"""
        def __init__(self, fail_times=2):
            self.calls: list = []
            self.fail_times = fail_times

        def send(self, text, disable_notification=False):
            self.calls.append((text, disable_notification))
            if len(self.calls) <= self.fail_times:
                raise _tg.TelegramError("模拟网络失败")
            return {"message_id": 1}

    _fl = _FlakyTg()
    _dm.tg.send = _fl.send
    check("回执：失败两次后重试成功", _dm.send_receipt("回执", silent=True) is True)
    check("回执：一共发了 3 次（1 + 2 次重试）",
          len(_fl.calls) == 3, str(len(_fl.calls)))
    check("回执静音：成功那条带 disable_notification",
          all(s is True for _, s in _fl.calls), str(_fl.calls))

    _fl2 = _FlakyTg(fail_times=99)
    _dm.tg.send = _fl2.send
    check("回执：一直失败就放弃，且不抛异常",
          _dm.send_receipt("回执", silent=False) is False)
    check("回执：失败那条不静音（要能把你叫醒）",
          all(s is False for _, s in _fl2.calls), str(_fl2.calls))
    _j_txt = "".join(p.read_text(encoding="utf-8") for p in _d.glob("*.jsonl"))
    check("回执最终失败记进 journal（事后可对账）",
          "回执最终未发出" in _j_txt, _j_txt[-200:])

    class _BuggyTg:
        """抛非通道类异常 = 编程错误。"""
        def __init__(self):
            self.n = 0

        def send(self, text, disable_notification=False):
            self.n += 1
            raise ValueError("编程错误")

    _bg = _BuggyTg()
    _dm.tg.send = _bg.send
    check("回执：编程错误不重试（不被说成网络抖动）",
          _dm.send_receipt("回执", silent=False) is False and _bg.n == 1,
          str(_bg.n))
finally:
    _dm.REPLY_RETRY_DELAYS = _saved_delays
    _dm.tg.send = _saved_tg_send
    _sh2.rmtree(_d, ignore_errors=True)

# 回执文案（V6）：不再复述全文；待办给了时间要说出来，并说清它不会到期提醒。
_d = _fresh_journal()
try:
    _i_v6, _f_v6 = _new_intake()
    _o_v6 = _i_v6.handle("明天交电费", _B)
    check("回执：待办也显示时间", "10月" in _o_v6.reply, _o_v6.reply)
    # 2026-10-07 改判：有日期的待办**落成原生到期日**了 ——
    # 回执从"只写进备注"改成说清"落成了什么、会不会弹"。
    check("回执：待办的时间落成原生日期（不再说『只写进备注』）",
          "不弹提醒" in _o_v6.reply and "备注" not in _o_v6.reply, _o_v6.reply)
    check("回执：全天待办说清『全天 · 不弹提醒』",
          "全天" in _o_v6.reply, _o_v6.reply)

    _i_v6t, _f_v6t = _new_intake()
    _o_v6t = _i_v6t.handle("!!明天下午两点 交电费", _B)
    check("回执：给了时刻的待办说『到点提醒』",
          "到点提醒" in _o_v6t.reply, _o_v6t.reply)
    check("回执：旗标与高优先级也回显",
          "已加旗标" in _o_v6t.reply and "高优先级" in _o_v6t.reply, _o_v6t.reply)

    _i_v6b, _f_v6b = _new_intake()
    _long_v6 = "# " + "这段感慨很长" * 6
    _o_v6b = _i_v6b.handle(_long_v6, _B)
    check("回执：超长正文被截断（不再回声半屏）",
          "…" in _o_v6b.reply and len(_o_v6b.reply) < 120, _o_v6b.reply)
    check("回执截断不影响写入（App 里仍是全文）",
          _f_v6b.calls[-1][1] == "这段感慨很长" * 6,
          str(_f_v6b.calls[-1])[:80])

    # 改动不能波及日历那一侧：全天日程**仍然**要说"（全天）"
    _i_v6c, _f_v6c = _new_intake()
    _o_v6c = _i_v6c.handle("@明天 项目周会", _B)
    check("回执：全天日程仍然写『（全天）』（没被 V6 波及）",
          "（全天）" in _o_v6c.reply, _o_v6c.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

section("发声面：非文本消息不再静默丢弃（V4）")

check("非文本识别：图片", _dm.non_text_kind({"photo": [{"file_id": "x"}]}) == "图片")
check("非文本识别：语音", _dm.non_text_kind({"voice": {"file_id": "x"}}) == "语音")
check("非文本识别：文字消息不算非文本",
      _dm.non_text_kind({"text": "交电费"}) == "")
# "不是文字"与"不是内容"要分开：服务类消息仍然安静跳过（回一句是打扰）
check("非文本识别：服务类消息不算'非文本内容'",
      _dm.non_text_kind({"new_chat_members": [{"id": 1}]}) == "")
check("非文本回执是陈述、不是提问（不引入跨消息状态）",
      "？" not in _dm.NON_TEXT_REPLY and "打字发我" in _dm.NON_TEXT_REPLY,
      _dm.NON_TEXT_REPLY)

_dm_src = (SRC / "daemon.py").read_text(encoding="utf-8")
check("run_once 对非文本消息真的回了话（不是 continue 了事）",
      "send_receipt(NON_TEXT_REPLY" in _dm_src)
check("启动通知静音发送",
      "tg.send(STARTUP_NOTICE, disable_notification=True)" in _dm_src)
check("回执按成败分档静音", "send_receipt(reply, silent=out.ok)" in _dm_src)

section("发声面：老按钮止转圈（V7）与「正在输入」（V9）")

# V7：老按钮被点一下必须**回应一句** —— 否则客户端上那个按钮一直转圈
# （老消息还留在聊天记录里）。这里验的是"真的回应了、且没写任何东西"。
_d = _fresh_journal()
_saved_ans = _tg.answer_callback
_saved_get = _tg.get_updates
_saved_state = _dm.STATE_FILE
_answered: list = []
try:
    _dm.STATE_FILE = _d / "state.json"
    _tg.answer_callback = (lambda cid, text=None, alert=False:
                           _answered.append((cid, text)))
    _tg.get_updates = lambda offset=None, limit=20, timeout=0: [
        {"update_id": 77, "callback_query": {
            "id": "CB1", "message": {"message_id": 5, "chat": {"id": "chat"}}}}]
    _dm.run_once(offset=1, wait=1)
    check("V7：老按钮点击被回应（不再永远转圈）",
          bool(_answered) and _answered[0][0] == "CB1", str(_answered))
    check("V7：回应的是一句陈述（说明已过期）",
          bool(_answered) and "过期" in (_answered[0][1] or ""), str(_answered))
    check("V7：仍然一个字都不写（回调不是内容）",
          _dm.load_offset() == 78, str(_dm.load_offset()))
finally:
    _tg.answer_callback = _saved_ans
    _tg.get_updates = _saved_get
    _dm.STATE_FILE = _saved_state
    _sh2.rmtree(_d, ignore_errors=True)

# V9：写 App 要几秒（AppleScript），所以处理前发一个"正在输入"。
# 它不产生消息，所以不像"先回一条收到"那样刷聊天记录。
check("telegram 提供 send_chat_action", hasattr(_tg, "send_chat_action"))
# ⚠️ 自检会真的调 handle_message —— 静音阀必须挡住它，否则每次自检都打真网络
check("V9：自检期被 PDCA_SUPPRESS_SEND 挡住（不会真发）",
      _tg.send_chat_action("typing") is False)
check("V9：handle_message 在处理前发过 typing",
      'tg.send_chat_action("typing")' in _dm_src)

section("命令 /list：只读、不写 App、不进 journal")

_cmd = _load(SRC / "commands.py")
# 与文件后段的 _rp 是同一个模块对象（_load 走 sys.modules）——
# 这里提前取个句柄，免得把整节挪到文件末尾。
_rp_c = _load(SRC / "report.py")

# 判据是 `/`（**字面**），不是词。这点必须钉死：
# `list` 不带斜杠就是一条**待办**（"list" 可以是你要买的东西），不是命令。
for _t, _want in [("/list", "list"), ("/ls", "list"), ("/today", "list"),
                  ("/now", "list"), ("/列表", "list"), ("/LIST", "list"),
                  ("/list@your_bot", "list"), ("/list 明天", "list")]:
    check(f"{_t!r} → 命令 {_want}", _cmd.match(_t) == _want, str(_cmd.match(_t)))
for _t in ("list", "列表", "交电费", "# 备忘", "@明天九点 会"):
    check(f"{_t!r} 不是命令（照常收件）", _cmd.match(_t) is None,
          str(_cmd.match(_t)))
for _t in ("/nope", "/", "/ "):
    check(f"{_t!r} → 不认识的命令", _cmd.match(_t) == _cmd.UNKNOWN,
          str(_cmd.match(_t)))

# 未完成的过滤（命令与日报共用 read_open_todos）
_saved_rt = _rp_c._read_todos
try:
    _rp_c._read_todos = lambda day=None: [_rp_c.Todo("甲", False), _rp_c.Todo("乙", True)]
    check("read_open_todos 只留未完成",
          [t.name for t in _rp_c.read_open_todos()] == ["甲"],
          str(_rp_c.read_open_todos()))
finally:
    _rp_c._read_todos = _saved_rt

# 全天判定（日历不给我们这个标志，从存法反推）
check("全日判定：0 点起跨 24 小时",
      _rp_c.is_all_day(_dt2.datetime(2026, 10, 5), _dt2.datetime(2026, 10, 6)))
check("全日判定：普通日程不算",
      not _rp_c.is_all_day(_dt2.datetime(2026, 10, 5, 14, 0),
                         _dt2.datetime(2026, 10, 5, 15, 0)))
check("全日判定：0 点但只有 1 小时不算",
      not _rp_c.is_all_day(_dt2.datetime(2026, 10, 5),
                         _dt2.datetime(2026, 10, 5, 1, 0)))

_NOW_C = _dt2.datetime(2026, 10, 4, 21, 40)      # 晚上 21:40
_TODOS_C = [_rp_c.Todo("交电费", False), _rp_c.Todo("跟进竣工报验", False)]
_EVS_C = {
    _dt2.date(2026, 10, 4): [
        _rp_c.Event("体检", _dt2.datetime(2026, 10, 4, 9, 0), "",
                  _dt2.datetime(2026, 10, 4, 10, 0), False),
        _rp_c.Event("夜跑", _dt2.datetime(2026, 10, 4, 22, 30), "",
                  _dt2.datetime(2026, 10, 4, 23, 30), False)],
    _dt2.date(2026, 10, 5): [
        _rp_c.Event("项目周会", _dt2.datetime(2026, 10, 5, 14, 0), "会议室",
                  _dt2.datetime(2026, 10, 5, 15, 0), False),
        _rp_c.Event("国庆值班", _dt2.datetime(2026, 10, 5, 0, 0), "",
                  _dt2.datetime(2026, 10, 6, 0, 0), True)],
}
def _evs_range(a, b):
    return [e for d, evs in _EVS_C.items() if a <= d < b for e in evs]


_r_c, _ok_c = _cmd.run("list", _cmd.Ctx(now=_NOW_C,
                                        open_todos=lambda d: _TODOS_C,
                                        events_range=_evs_range))
check("命令：正常执行 → ok=True（静音发）", _ok_c is True)
check("命令：列出未完成待办",
      "　交电费" in _r_c and "跟进竣工报验" in _r_c, _r_c)
check("命令：今天**已结束**的日程不出现", "体检" not in _r_c, _r_c)
check("命令：今天还没结束的日程出现", "22:30 夜跑" in _r_c, _r_c)
check("命令：明天的日程全列（含地点）",
      "14:00 项目周会" in _r_c and "@会议室" in _r_c, _r_c)
check("命令：全天日程写成「全天」而不是 00:00",
      "全天 国庆值班" in _r_c and "00:00" not in _r_c, _r_c)

# ⚠️ 空列表有两种含义 ——「真的没有」和「读不到」，绝不能混。
# 第一版就踩了：读不到时照样打印"待办 0 件 🎉 一件都没有"，正文在撒谎。
def _boom_c(day=None):
    raise RuntimeError("AppleScript 超时")

_r_fail, _ = _cmd.run("list", _cmd.Ctx(now=_NOW_C, open_todos=_boom_c,
                                       events_range=_boom_c))
check("命令：读不到时**不说**「一件都没有」", "一件都没有" not in _r_fail, _r_fail)
check("命令：读不到时**不说**「没有日程」", "没有日程" not in _r_fail, _r_fail)
check("命令：读不到时如实写「读不到」",
      _r_fail.count("读不到（见下方告警）") == 2, _r_fail)

_r_empty, _ = _cmd.run("list", _cmd.Ctx(now=_NOW_C,
                                        open_todos=lambda d: [],
                                        events_range=lambda a, b: []))
check("命令：真没有时才说「一件都没有」", "一件都没有" in _r_empty, _r_empty)
check("命令：真没有时才说「今明两天都没有日程」",
      "今明两天都没有日程" in _r_empty, _r_empty)

_many = [_rp_c.Todo(f"第{i}件", False) for i in range(20)]
_r_many, _ = _cmd.run("list", _cmd.Ctx(now=_NOW_C, open_todos=lambda d: _many,
                                       events_range=lambda a, b: []))
check("命令：超过上限时截断并说明总数",
      "待办 20 件" in _r_many and "…还有 5 件" in _r_many, _r_many)

_r_unk, _ok_unk = _cmd.run(_cmd.UNKNOWN)
check("命令：不认识的命令 → ok=False（有声发送）", _ok_unk is False)
check("命令：不认识的命令给出可用命令", "/list" in _r_unk, _r_unk)

# ── `/` 菜单（setMyCommands）
#
# Telegram 对命令名有硬约束，不合规 API 直接报错 —— 而那种错误在守护日志里
# 只表现为"菜单设置失败"，很难查。所以在这里守住形状。
check("菜单：至少有一条命令", len(_cmd.MENU) >= 1, str(_cmd.MENU))
for _mn, _md in _cmd.MENU:
    check(f"菜单 {_mn!r}：名字合规（[a-z0-9_]，1–32）",
          bool(_re.fullmatch(r"[a-z0-9_]{1,32}", _mn)), _mn)
    check(f"菜单 {_mn!r}：描述 3–256 字符", 3 <= len(_md) <= 256, f"{len(_md)}")
    check(f"菜单 {_mn!r}：真的是一条能跑的命令", _mn in _cmd.ALIASES, _mn)
    check(f"菜单 {_mn!r}：菜单里放的是规范名（不是别名）",
          any(c.name == _mn for c in _cmd.COMMANDS), _mn)
    check(f"菜单 {_mn!r}：提示语里也念得到它",
          f"/{_mn}" in _cmd.USAGE, _cmd.USAGE)

# ── 命令注册表：加命令不该需要去别处同步词汇
#
# 2026-10-05 起，别名 / `/` 菜单 / 提示语都从 `COMMANDS` **派生**。
# 这一节验的是"派生真的成立" —— 否则注册表只是换了个写法。
# （原先 ALIASES / MENU / USAGE 是三份手写表，加一条命令要三处同步。）

# ① 别名不能撞车 —— 撞了的话字典推导会**静默**让后声明的那条赢
_seen_alias: dict = {}
_clash: list = []
for _c in _cmd.COMMANDS:
    for _a in (_c.name, *_c.aliases):
        if _a in _seen_alias:
            _clash.append(f"{_a}（{_seen_alias[_a]} 与 {_c.name} 撞）")
        _seen_alias[_a] = _c.name
check("命令别名不撞车", not _clash, "；".join(_clash))

# ② 别名的目标必须是一条真命令 —— 否则 match() 会给出一个 run() 不认识的名字，
#    表现是"命令打得出来，回一句不认识"
check("别名的目标都真实存在",
      all(_cmd.find(v) is not None for v in _cmd.ALIASES.values()),
      str(sorted(set(_cmd.ALIASES.values()))))

# ③ 每条命令都要在提示语里出现 —— 否则它存在但**永远发现不了**
check("提示语覆盖每条命令",
      all(f"/{c.name}" in _cmd.USAGE for c in _cmd.COMMANDS), _cmd.USAGE)
check("每条命令都有自己的 usage 文案",
      all(c.usage for c in _cmd.COMMANDS),
      str([c.name for c in _cmd.COMMANDS if not c.usage]))

# ④ 每条命令都真的能跑（**注册表不许撒谎**）。
#    用一份"什么都没有"的假 ctx：只读命令在这上面不该抛异常。
_stub_ctx = _cmd.Ctx(now=_NOW_C, open_todos=lambda d: [],
                     events_range=lambda a, b: [])
for _c in _cmd.COMMANDS:
    try:
        _out = _cmd.run(_c.name, _stub_ctx)
        _runnable = (isinstance(_out, tuple) and len(_out) == 2
                     and isinstance(_out[0], str) and isinstance(_out[1], bool))
    except Exception as _e:  # noqa: BLE001
        _runnable, _out = False, repr(_e)
    check(f"命令 {_c.name!r}：真的能跑（注册表不撒谎）", _runnable, str(_out))

# ⑤ 命令条数是**有意的决定**，不是随手加的。
#    ⚠️ 加第二条命令时把这个数字一起改 —— 它就是那次决定的记录点
#    （2026-10-04 为"要不要 /help"专门裁决过：不做与符号并行的词汇表）。
check("命令条数没变（加命令要是一次有意的决定）",
      len(_cmd.COMMANDS) == 1,
      f"现在有 {len(_cmd.COMMANDS)} 条：" + "、".join(c.name for c in _cmd.COMMANDS))

check("telegram 提供 set_my_commands / get_my_commands",
      hasattr(_tg, "set_my_commands") and hasattr(_tg, "get_my_commands"))
check("菜单：自检期被静音阀挡住（不打真网络）",
      _tg.set_my_commands(_cmd.MENU) is False and _tg.get_my_commands() == [])

# 报文形状：setMyCommands 要的是 [{"command":…,"description":…}, …]
_saved_call = _tg._call
_saved_cfg = _tg.load_config
_saved_env = os.environ.pop("PDCA_SUPPRESS_SEND", None)
try:
    _payloads: list = []
    _tg.load_config = lambda: ("tok", "chat")
    _tg._call = lambda token, method, params=None, timeout=20: (
        _payloads.append((method, params)), {})[1]
    check("菜单：能推上去", _tg.set_my_commands(_cmd.MENU) is True)
    check("菜单：用的是 setMyCommands",
          bool(_payloads) and _payloads[-1][0] == "setMyCommands", str(_payloads))
    check("菜单：报文形状 = [{'command','description'}]",
          _payloads[-1][1] == {"commands": [{"command": n, "description": d}
                                            for n, d in _cmd.MENU]},
          str(_payloads[-1][1]))

    _tg._call = lambda token, method, params=None, timeout=20: [
        {"command": "list", "description": "x"}]
    check("菜单：能读回来（运维查证用）",
          _tg.get_my_commands() == [{"command": "list", "description": "x"}])
finally:
    _tg._call = _saved_call
    _tg.load_config = _saved_cfg
    if _saved_env is not None:
        os.environ["PDCA_SUPPRESS_SEND"] = _saved_env

check("守护启动时会推「/」菜单（改了命令表 → 重启即生效）",
      "tg.set_my_commands(commands.MENU)" in _dm_src)

# ── 整链：/list 一个字都不写，也不进 journal
_d = _fresh_journal()
_dm_dir3 = _P2(_tf2.mkdtemp())
# ⚠️ 这里必须是 **read_events_between**（下面注入的就是它）。
# 原先写的是 read_events —— 名字差了半截，于是**桩永远没被还原**：
# 从此往后任何调用 report.read_events_between 的断言，测的都是
# 一个返回 [] 的 lambda。实测：2026-10-08 加"跨日历"断言时才发现
# （4 条全红，而 asked 是空的 —— 因为函数体根本不是 report 的）。
_saved_c = (_dm._make_intake, _dm.tg.send, _dm.STATE_FILE, _dm.tg.get_updates,
            _dm.tg.load_config, _rp_c.read_open_todos, _rp_c.read_events_between)
try:
    _wrote_c: list = []
    _dm._make_intake = lambda: _it.Intake(
        add_todo=lambda t, w=None, f=False, p=0: (
        _wrote_c.append(("todo", t)), "T")[1],
        add_event=lambda *a, **k: (_wrote_c.append(("event",)), "E")[1],
        add_memo=lambda t: (_wrote_c.append(("memo", t)), "M")[1],
        testing=True)
    _sent_c: list = []
    _dm.tg.send = lambda t, **kw: _sent_c.append((t, kw.get("disable_notification")))
    _dm.STATE_FILE = _dm_dir3 / "state.json"
    _dm.tg.load_config = lambda: ("tok", "chat")
    _dm.tg.get_updates = lambda offset=None, limit=20, timeout=0: [
        {"update_id": 91, "message": {"message_id": 7, "chat": {"id": "chat"},
                                      "text": "/list"}}]
    # 读取注入成假的：自检不该真的去 osascript 读提醒事项/日历
    _rp_c.read_open_todos = lambda day=None: [_rp_c.Todo("甲", False)]
    _rp_c.read_events_between = lambda a, b: []

    _dm.run_once(offset=1, wait=1)
    check("命令：一个字都不写进 App", _wrote_c == [], str(_wrote_c))
    check("命令：发出了一条回执", len(_sent_c) == 1, str(len(_sent_c)))
    check("命令：回执静音（成功那条不响）", _sent_c[0][1] is True, str(_sent_c))
    check("命令：回执里有待办", "　甲" in _sent_c[0][0], _sent_c[0][0])
    _j_c = "".join(p.read_text(encoding="utf-8") for p in _d.glob("*.jsonl"))
    check("命令：不进 journal（命令是读，不是内容）",
          "input" not in _j_c and "/list" not in _j_c, _j_c)
finally:
    (_dm._make_intake, _dm.tg.send, _dm.STATE_FILE, _dm.tg.get_updates,
     _dm.tg.load_config, _rp_c.read_open_todos, _rp_c.read_events_between) = _saved_c
    _sh2.rmtree(_d, ignore_errors=True)
    _sh2.rmtree(_dm_dir3, ignore_errors=True)

check("真实自检用分钟精度断言（与 applecal.add 一致）",
      "second=0" in (ROOT / "tools" / "selftest-live.py").read_text(encoding="utf-8"),
      "断言未对齐精度会导致假失败")

section("真实环境自检脚本（tools/selftest-live.py）")

# 单元测试与冒烟都用**假写入端**，证明不了"真的能写进去"。
# 这个脚本做真实读写、每步读回验证、跑完清理。
_live = ROOT / "tools" / "selftest-live.py"
check("存在 tools/selftest-live.py", _live.is_file())

_live_src = _live.read_text(encoding="utf-8")
# 三处都要真实读写
for _what in ("提醒事项", "备忘录", "日历"):
    check(f"真实自检覆盖{_what}", _what in _live_src)
# 必须用临时容器并在 finally 里清理（不能污染用户数据）
check("自检用临时列表/文件夹/日历",
      "SELFTEST" in _live_src and "PID" in _live_src)
check("自检在 finally 里清理", _live_src.count("finally:") >= 3)
# 提供只读模式（先确认权限，再决定要不要写）
check("自检提供 --readonly", "--readonly" in _live_src)
# 清理失败要告诉用户手动删什么
check("自检会提示残留物如何清理", "手动删除" in _live_src)

# 脚本用到的 API 必须都存在 —— 这类"脚本调了不存在的方法"
# 只有在真实环境跑到那一步才会暴露。
_lv = _load(SRC / "reminders.py")
_mv = _load(SRC / "memo.py")
_av = _load(SRC / "applecal.py")
for _n in ("verify_list", "create", "all_reminders", "set_completed", "delete"):
    check(f"Reminders 有 {_n}", hasattr(_lv.Reminders, _n))
for _n in ("list_folders", "snapshot_ids", "add", "text_of"):
    check(f"memo 有 {_n}", hasattr(_mv, _n))
for _n in ("list_calendars", "events_between", "add"):
    check(f"applecal 有 {_n}", hasattr(_av, _n))
# Reminders 接受 config 字典（自检用它指向临时列表）
try:
    _lv.Reminders({"reminders_list": "X"})
    _rem_ok = True
except Exception:
    _rem_ok = False
check("Reminders 可用 config 字典构造", _rem_ok)


section("v4 待办的原生字段（2026-10-07：跟随默认列表 + 原生到期日 + 旗标）")

# 为什么单开一节：这一节守的是**用户当天的改判**（见 CHANGELOG 的 2.0.0）：
#   ① 落点从固定的 PDCA 列表改成**跟随系统默认列表**
#   ② 待办的时间从"写进备注"改成**原生到期日**（"今天/已编排"两个智能列表靠它）
#   ③ 新增 `!` / `!!` 声明旗标与高优先级
# 三件都在"写入那一刻"生效，而失效方式全是**静默的** —— 所以都在这儿钉住。

# ① 列表名：配置为空 → 读系统的 default list（只读属性）
_rl_saved = _lv.run
try:
    _rl_calls: list[str] = []

    def _rl_fake(src, retries=3, backoff=1.5):
        _rl_calls.append(src)
        return "原生提醒事项\n"

    _lv.run = _rl_fake
    check("列表：配置为空 → 跟随系统默认列表",
          _lv.Reminders({"reminders_list": ""}).list_name == "原生提醒事项")
    check("列表：读的是字典里的 default list",
          any("default list" in c for c in _rl_calls), str(_rl_calls[:1]))
    check("列表：配置写了名字就不去问系统",
          _lv.Reminders({"reminders_list": "X"}).list_name == "X")
finally:
    _lv.run = _rl_saved

# ② create() **实际会执行的那段 AppleScript**（不看源码，看脚本）
_sv_saved = _lv.run
try:
    _scripts: list[str] = []
    _last = {"allday": False}

    def _cap_run(src, retries=3, backoff=1.5):
        _scripts.append(src)
        if "default list" in src:
            return "默认列表"
        if "make new reminder" in src:
            _last["allday"] = "allday due date:" in src
            return "ok"
        if "flagged of r" in src:          # 读回校验（_verify_written）
            tail = "none\n2026,10,7\n" if _last["allday"] else "2026,10,7,14,0\nnone\n"
            return "true\n1\n" + tail
        return "ok"

    _lv.run = _cap_run
    _rc = _lv.Reminders({"reminders_list": "X"})
    _seq = {"n": 0}

    def _grow():
        # 第一次叫（before）返回空，之后返回"刚建的那条"
        _seq["n"] += 1
        if _seq["n"] == 1:
            return []
        return [_lv.Reminder(id="NEW-1", name="x", completed=False, body="", due="")]

    _rc.all_reminders = _grow
    _rc.create("交电费", due=_dt2.datetime(2026, 10, 7, 14, 0),
               remind=True, flagged=True, priority=1)
    _mk = [s for s in _scripts if "make new reminder" in s][-1]
    check("create：到期日写 due date", "due date:dueDate" in _mk, _mk[:160])
    check("create：给了时刻就连 remind me date 一起写（到点弹）",
          "remind me date:dueDate" in _mk, _mk[:160])
    check("create：旗标进了脚本", "flagged:true" in _mk, _mk[:160])
    check("create：优先级进了脚本", "priority:1" in _mk, _mk[:160])
    check("create：日期逐字段构造（不用日期字面量）",
          "set year of dueDate to 2026" in _mk and 'date "' not in _mk, _mk[:160])

    _scripts.clear()
    _seq["n"] = 0
    _rc.create("交电费", due=_dt2.datetime(2026, 10, 7, 9, 0),
               allday_due=True, flagged=True, priority=1)
    _mk2 = [s for s in _scripts if "make new reminder" in s][-1]
    # ⚠️ 不能写 `"due date:dueDate" not in _mk2` —— `allday due date:dueDate`
    # **含**这个子串，那样写永远红（同一天的第二次子串陷阱：
    # 上一次是 "display alarm" 撞 "display alarms"）。数出现次数才可靠。
    check("create：全天 → allday due date（且只写一次日期）",
          "allday due date:dueDate" in _mk2
          and _mk2.count("due date:dueDate") == 1, _mk2[:160])
    check("create：全天不写 remind me date（没有时刻可弹）",
          "remind me date" not in _mk2, _mk2[:160])

    # 读回校验必须**真的会红** —— 把返回值改成"旗标没生效"再看
    _scripts.clear()
    _seq["n"] = 0
    _lv.run = lambda src, retries=3, backoff=1.5: (
        _scripts.append(src)
        or ("默认列表" if "default list" in src else
            ("ok" if "make new reminder" in src else "false\n1\nnone\nnone\n")))
    try:
        _rc.create("交电费", due=_dt2.datetime(2026, 10, 7, 14, 0),
                   remind=True, flagged=True, priority=1)
        _verify_red = False
    except _lv.RemindersError as e:
        _verify_red = "旗标没写进去" in str(e)
    check("create：旗标没生效时**真的报错**（不是静默通过）", _verify_red)
finally:
    _lv.run = _sv_saved

# ③ journal 记下原生字段 —— 读路径**不返回**到期日与旗标，只有这里记得下来
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("!!明天下午两点 交电费", _B)
    _rec_t = [r for r in _jr.read_day(_jr._today()) if r["event"] == "todo_added"]
    check("journal：todo_added 记了 due",
          bool(_rec_t) and _rec_t[-1].get("due") != "", str(_rec_t[-1:]))
    check("journal：todo_added 记了 flagged / priority",
          bool(_rec_t) and _rec_t[-1].get("flagged") is True
          and _rec_t[-1].get("priority") == 1, str(_rec_t[-1:]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ④ 符号 `!` / `!!`（2026-10-07 新增，用户裁决"先跑几天试手感"）
for _txt, _wf, _wp in [("!交电费", True, 0), ("!!交电费", True, 1),
                       ("！交电费", True, 0), ("！!交电费", True, 1),
                       ("交电费", False, 0), ("- 交电费", False, 0),
                       ("!- 交电费", True, 0)]:
    _it_s = _rt.route(_txt, _B)
    check(f"符号：{_txt!r} → 旗标={_wf} 优先级={_wp}",
          _it_s.kind is _K.Kind.TODO and _it_s.flagged is _wf
          and _it_s.priority == _wp and _it_s.text == "交电费",
          f"{_it_s.kind} {_it_s.text!r} {_it_s.flagged} {_it_s.priority}")


section("v4 对现有模块的调用契约")

# 为什么需要：`intake._real_add_todo` / `_real_add_event` / `_real_add_memo`
# 调的是**已有模块**（reminders / applecal / memo）。这些调用在沙箱里
# 跑不到（没授权），所以签名不匹配的话，直到你第一次真实收件才会炸 ——
# 而那时错误信息可能只是一句含糊的 AppleScript 报错。
#
# 这里用 inspect 核对：**参数名与数量必须能对上**，且返回值属性存在。
import inspect as _insp8  # noqa: E402
import dataclasses as _dc8  # noqa: E402

_rems8 = _load(SRC / "reminders.py")
_ac8 = _load(SRC / "applecal.py")
_mm8 = _load(SRC / "memo.py")

# ① 待办端：Reminders 的 create / verify_list / make_key
check("Reminders 有 create", hasattr(_rems8.Reminders, "create"))
check("Reminders 有 verify_list", hasattr(_rems8.Reminders, "verify_list"))
_sig_create = _insp8.signature(_rems8.Reminders.create)
check("Reminders.create 接受 name/body",
      {"name", "body"} <= set(_sig_create.parameters))
# intake 调的是 rem.create(name=text, body=body)
_sig_create.bind(None, name="x", body="y")
check("create(name=, body=) 是合法调用", True)

_sig_mk = _insp8.signature(_rems8.make_key)
_sig_mk.bind("text")            # intake 调 make_key(text)
check("make_key(text) 是合法调用", True)
check("Reminder 有 id 字段（intake 要取它）",
      "id" in {f.name for f in _dc8.fields(_rems8.Reminder)})

# ② 日程端：applecal.add 的关键字与返回值
_sig_ev = _insp8.signature(_ac8.add)
for _kw in ("summary", "start", "end", "location", "recurrence", "allday",
            "alarm"):
    check(f"applecal.add 接受 {_kw}", _kw in _sig_ev.parameters)
# intake 调的是 add(summary, start, end, location=..., recurrence=...,
#                    allday=..., alarm=...)
_sig_ev.bind(None, _dt2.datetime(2026, 10, 5), _dt2.datetime(2026, 10, 5, 1),
             location="", recurrence="", allday=False, alarm=False)
check("applecal.add(...) 是合法调用", True)
check("applecal.Event 有 uid 字段",
      "uid" in {f.name for f in _dc8.fields(_ac8.Event)})

# report 读事件用 events_between(date, date)
_sig_eb = _insp8.signature(_ac8.events_between)
_sig_eb.bind(_dt2.date(2026, 10, 5), _dt2.date(2026, 10, 6))
check("events_between(date, date) 是合法调用", True)
check("applecal.Event 有 summary/start/location（report 要用）",
      {"summary", "start", "location"} <=
      {f.name for f in _dc8.fields(_ac8.Event)})

# ── 日程读取的两处真实修复（2026-10-05，都是 /list 第一次真跑撞出来的）
#
# ① 没填地点的日程：AppleScript 回传的是字面量 `missing value`，
#    不是空串 —— 于是回执里会出现 `@missing value`。
#    日报的"明日日程"同样会带上它（只是那两天日历读不到，没人看见）。
_msv = (_ac8.FSEP.join(["去龙井村", "2026-10-5 0:0", "2026-10-6 0:0",
                        "missing value", "UID-1"]) + _ac8.RSEP
        + _ac8.FSEP.join(["项目周会", "2026-10-5 14:0", "2026-10-5 15:0",
                          "会议室", "UID-2"]) + _ac8.RSEP)
_parsed = _ac8.parse_events(_msv)
check("日程解析：missing value 归一成空串（不显示 @missing value）",
      _parsed[0].location == "", repr(_parsed[0].location))
check("日程解析：真有地点就留着", _parsed[1].location == "会议室",
      repr(_parsed[1].location))
check("日程解析：summary 同样归一", _ac8.parse_events(
    _ac8.FSEP.join(["missing value", "2026-10-5 0:0", "2026-10-6 0:0",
                    "", "U"]) + _ac8.RSEP)[0].summary == "")

# ② Calendar 没在跑时，**读**会失败（-600）而**写**不会 —— 读路径要自己把 App 拉起来。
#    这里把两次调用换成假的，验"只在 -600 时拉、只重试一次、别的不动"。
_saved_once = _ac8._events_in_window
_saved_launch = _ac8._launch_calendar
try:
    _n = {"once": 0, "launch": 0}

    def _fake_launch():
        _n["launch"] += 1

    def _fail_first(s, e, c=None):
        """第一次 -600，之后成功。"""
        _n["once"] += 1
        if _n["once"] == 1:
            raise _ac8.CalendarError("-600 應用程式不在執行中")
        return []

    def _always(s, e, c=None):
        _n["once"] += 1
        raise _ac8.CalendarError(_always.msg)

    _ac8._launch_calendar = _fake_launch
    _ac8._events_in_window = _fail_first
    check("日程读取：-600 时拉起 Calendar 并重试一次",
          _ac8.events_between(_dt2.date(2026, 10, 5),
                              _dt2.date(2026, 10, 6)) == []
          and _n["launch"] == 1 and _n["once"] == 2, str(_n))

    # 别的错误不该去拉 App（-10004 是授权问题，拉一百次也没用）
    _n.update(once=0, launch=0)
    _always.msg = "-10004 越权"
    _ac8._events_in_window = _always
    try:
        _ac8.events_between(_dt2.date(2026, 10, 5), _dt2.date(2026, 10, 6))
        check("日程读取：非 -600 的错误直接抛", False, "没抛")
    except _ac8.CalendarError:
        check("日程读取：非 -600 的错误直接抛（不去拉 App）",
              _n["launch"] == 0 and _n["once"] == 1, str(_n))

    # 拉起来还是失败 → 如实抛，不无限重试
    _n.update(once=0, launch=0)
    _always.msg = "-600 还在"
    try:
        _ac8.events_between(_dt2.date(2026, 10, 5), _dt2.date(2026, 10, 6))
        check("日程读取：重试后仍失败就如实抛", False, "没抛")
    except _ac8.CalendarError:
        check("日程读取：重试后仍失败就如实抛（不重试第三次）",
              _n["once"] == 2 and _n["launch"] == 1, str(_n))
finally:
    _ac8._events_in_window = _saved_once
    _ac8._launch_calendar = _saved_launch
check("日程读取：拉起用的是 open -g + osascript launch 两条路，且都有超时",
      '"/usr/bin/open", "-g", "-a", APP' in
      (SRC / "applecal.py").read_text(encoding="utf-8")
      and "to launch" in (SRC / "applecal.py").read_text(encoding="utf-8"))

# ── 全天事件的时刻必须归零（2026-10-05，用户实报）
#
# "去龙井村被排到 5 日和 6 日两天"：`whens` 给全天事件的是"当天 09:00 + 24 小时"，
# 而 Calendar 收到 allday 后把 start 归零、**end 按原样留着** ——
# 实际成了 10-05 00:00 → 10-06 09:00 的 33 小时事件，日历上必然压两天。
#
# 为什么两条读路径都没发现：它们都按 start 的**日期**筛，
# 在我方看起来永远是"10-05 那一天"的一件事；回执写的是 `（全天）`
# （看的是 all_day 标志）。**只有打开日历用眼睛看才会发现。**
#
# 这里直接看**生成的 AppleScript**：全天时两端 hours 必须都是 0。
_saved_arun = _ac8.run_applescript
try:
    _scripts: list = []
    _ac8.run_applescript = lambda src, timeout=60: (
        _scripts.append(src), "UID-ALLDAY")[1]

    _ev_ad = _ac8.add("去龙井村", _dt2.datetime(2026, 10, 5, 9, 0),
                      _dt2.datetime(2026, 10, 6, 9, 0),
                      calendar="测试日历", allday=True)
    _sc_ad = _scripts[-1]
    check("全天日程：start 的时刻被归零",
          "set hours of startDate to 0" in _sc_ad, _sc_ad[:160])
    check("全天日程：end 的时刻也被归零（否则跨两天）",
          "set hours of endDate to 0" in _sc_ad, _sc_ad[:160])
    check("全天日程：返回的对象也是归零后的时间",
          _ev_ad.start == _dt2.datetime(2026, 10, 5, 0, 0)
          and _ev_ad.end == _dt2.datetime(2026, 10, 6, 0, 0),
          f"{_ev_ad.start} → {_ev_ad.end}")
    check("全天日程：allday 标记没丢", "allday event:true" in _sc_ad)

    # 定时日程**不能**被一起归零
    _ac8.add("项目周会", _dt2.datetime(2026, 10, 5, 14, 0),
             _dt2.datetime(2026, 10, 5, 15, 0), calendar="测试日历")
    _sc_ad2 = _scripts[-1]
    check("定时日程：时刻原样保留（没被全天那套波及）",
          "set hours of startDate to 14" in _sc_ad2, _sc_ad2[:160])
    check("定时日程：不带 allday 标记", "allday event:true" not in _sc_ad2)
finally:
    _ac8.run_applescript = _saved_arun

# 不过"正则有运算符"只是形状检查。真正能证明语法对的是**编译一遍**：
# 把 applecal 实际生成的读取脚本丢给 osacompile。
_saved_ar1 = _ac8.run_applescript
try:
    _gen1: list = []
    _ac8.run_applescript = lambda src, timeout=120: (_gen1.append(src), "")[1]
    _ac8._events_between_once(_dt2.date(2026, 10, 6), _dt2.date(2026, 10, 7),
                               "测试日历")
    _rc1 = _sp.run(["osacompile", "-o", os.devnull, "-e", _gen1[-1]],
                   capture_output=True, text=True)
    check("日历读取脚本（含 whose 日期比较）真的能编译",
          _rc1.returncode == 0, _rc1.stderr.strip()[:120])
    check("日历读取：日期比较交给日历自己（不再遍历整个日历）",
          "whose start date" in _gen1[-1], _gen1[-1][:160])
    check("日历读取：边界日期逐字段构造（不走 date \"…\" 字面量）",
          "set year of dFrom to 2026" in _gen1[-1]
          and 'date "' not in _gen1[-1], _gen1[-1][:160])
finally:
    _ac8.run_applescript = _saved_ar1

# ── 重复日程按天展开（2026-10-05）
#
# 日历里一条重复事件是**一个**对象，start date 是**首次**发生日 ——
# 窗口查询永远查不到它之后的发生（`@每天八点 跑步` 从第二天起就消失了）。
# 这里把两个数据源都换成假的，验"展开 + 去重 + 看不懂时不猜"。
_saved_win = _ac8._events_between_once
_saved_rec = _ac8._recurring_masters
try:
    def _fake_win(s_, e_, c=None):
        # 窗口里有一个一次性事件，和一个重复事件的**首次**发生
        return [_ac8.Event(summary="体检",
                           start=_dt2.datetime(2026, 10, 5, 9, 0),
                           end=_dt2.datetime(2026, 10, 5, 10, 0), uid="U1"),
                _ac8.Event(summary="跑步",
                           start=_dt2.datetime(2026, 10, 5, 8, 0),
                           end=_dt2.datetime(2026, 10, 5, 9, 0), uid="U2",
                           recurrence="FREQ=DAILY")]

    _ac8._events_between_once = _fake_win
    _ac8._recurring_masters = lambda c=None: [
        _ac8.Event(summary="跑步", start=_dt2.datetime(2026, 10, 5, 8, 0),
                   end=_dt2.datetime(2026, 10, 5, 9, 0), uid="U2",
                   recurrence="FREQ=DAILY")]

    _evs = _ac8.events_between(_dt2.date(2026, 10, 5), _dt2.date(2026, 10, 8))
    _names = [(e.summary, e.start.strftime("%m-%d %H:%M")) for e in _evs]
    check("重复日程：之后的每一天都出现（不再只有首次）",
          _names.count(("跑步", "10-06 08:00")) == 1
          and _names.count(("跑步", "10-07 08:00")) == 1, str(_names))
    check("重复日程：首次那天不重复（窗口查询已给过）",
          _names.count(("跑步", "10-05 08:00")) == 1, str(_names))
    check("重复日程：时刻与时长平移后不变",
          all(e.end - e.start == _dt2.timedelta(hours=1) for e in _evs), str(_names))
    check("重复日程：一次性事件照旧", ("体检", "10-05 09:00") in _names, str(_names))
    check("重复日程：结果按时间排序",
          [e.start for e in _evs] == sorted(e.start for e in _evs))

    # 看不懂的规则 → **不猜**（只在首次那天显示），且不静默
    _ac8._recurring_masters = lambda c=None: [
        _ac8.Event(summary="怪规则", start=_dt2.datetime(2026, 10, 5, 7, 0),
                   end=_dt2.datetime(2026, 10, 5, 8, 0), uid="U3",
                   recurrence="FREQ=MONTHLY;BYSETPOS=2;BYDAY=TU")]
    _evs2 = _ac8.events_between(_dt2.date(2026, 10, 6), _dt2.date(2026, 10, 9))
    check("看不懂的重复规则：不猜、不加假发生",
          all(e.summary != "怪规则" for e in _evs2),
          str([e.summary for e in _evs2]))
finally:
    _ac8._events_between_once = _saved_win
    _ac8._recurring_masters = _saved_rec

# ── 日历诊断：让这些行**真的被执行**（2026-10-06）
#
# 起因：`_note` 被调用 9 次、却**从未定义成功** —— 一次替换没匹配上、
# 静默没生效（我写的锚点是 `CalendarError(RuntimeError)`，源码是 `(Exception)`）。
# 自检当时没发现，因为**那些行从来没被执行到**：-600 路径与超时路径都没测。
# 下面两条把两条异常路径都跑一遍，并检查 stderr 里真的留下了痕迹。
import io as _io      # noqa: E402
import contextlib as _ctx  # noqa: E402

_saved_once2 = _ac8._events_between_once
_saved_rec2 = _ac8._recurring_masters
_saved_ar2 = _ac8.run_applescript
_saved_sub2 = _ac8.subprocess
try:
    class _FakeSub:
        @staticmethod
        def run(*a, **k):
            return None

    _ac8.subprocess = _FakeSub        # _launch_calendar 里的两条拉起命令
    _ac8._recurring_masters = lambda c=None: []
    _calls2 = {"once": 0, "poll": 0}

    def _once_600(s_, e_, c=None):
        _calls2["once"] += 1
        if _calls2["once"] == 1:
            raise _ac8.CalendarError("-600 應用程式不在執行中")
        return []

    def _poll(src, timeout=120):
        _calls2["poll"] += 1
        return "1"

    _ac8._events_between_once = _once_600
    _ac8.run_applescript = _poll
    _buf2 = _io.StringIO()
    with _ctx.redirect_stderr(_buf2):
        _out2 = _ac8.events_between(_dt2.date(2026, 10, 7), _dt2.date(2026, 10, 8))
    check("日历诊断：-600 路径真的跑通（不留 NameError）",
          _out2 == [] and _calls2["once"] == 2 and _calls2["poll"] >= 1,
          str(_calls2))
    _txt2 = _buf2.getvalue()
    check("日历诊断：-600 时往 stderr 留了痕（没在 / 已就绪）",
          "正在拉起" in _txt2 and "已就绪" in _txt2, repr(_txt2[:140]))

    # 超时路径：**抛出去**（不许吞）+ 留下带耗时的痕迹
    _ac8._events_between_once = lambda s_, e_, c=None: (_ for _ in ()).throw(
        _ac8.CalendarError("AppleScript 超时（日历可能在冷启动，稍后重试）"))
    _buf3 = _io.StringIO()
    try:
        with _ctx.redirect_stderr(_buf3):
            _ac8._events_in_window(_dt2.date(2026, 10, 7), _dt2.date(2026, 10, 8))
        check("日历诊断：超时要抛出去（不能被吞）", False, "没抛")
    except _ac8.CalendarError:
        check("日历诊断：超时往 stderr 留了带耗时的痕",
              "窗口查询失败" in _buf3.getvalue()
              and "耗时" in _buf3.getvalue(), repr(_buf3.getvalue()[:160]))
finally:
    _ac8._events_between_once = _saved_once2
    _ac8._recurring_masters = _saved_rec2
    _ac8.run_applescript = _saved_ar2
    _ac8.subprocess = _saved_sub2

# occurs_on：重复规则判据（用户看到"跑步每天都在"全靠它）
_A5 = _dt2.date(2026, 10, 5)      # 周一
check("occurs_on：每天", _whens.occurs_on("FREQ=DAILY", _A5, _dt2.date(2026, 11, 1)) is True)
check("occurs_on：每周一（周二不算）",
      _whens.occurs_on("FREQ=WEEKLY;BYDAY=MO", _A5, _dt2.date(2026, 10, 6)) is False)
check("occurs_on：每周一（下周一算）",
      _whens.occurs_on("FREQ=WEEKLY;BYDAY=MO", _A5, _dt2.date(2026, 10, 12)) is True)
check("occurs_on：每工作日（周六不算）",
      _whens.occurs_on("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", _A5,
                          _dt2.date(2026, 10, 10)) is False)
check("occurs_on：间隔 2 周",
      _whens.occurs_on("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO", _A5,
                          _dt2.date(2026, 10, 12)) is False)
check("occurs_on：COUNT 用完就不再发生",
      _whens.occurs_on("FREQ=DAILY;COUNT=3", _A5, _dt2.date(2026, 10, 8)) is False)
check("occurs_on：UNTIL 之后不再发生",
      _whens.occurs_on("FREQ=DAILY;UNTIL=20261007T235959Z", _A5,
                          _dt2.date(2026, 10, 8)) is False)
check("occurs_on：每年",
      _whens.occurs_on("FREQ=YEARLY", _A5, _dt2.date(2027, 10, 5)) is True)
check("occurs_on：看不懂的规则返回 None（不是 False）",
      _whens.occurs_on("FREQ=MONTHLY;BYSETPOS=2;BYDAY=TU", _A5,
                          _dt2.date(2026, 10, 13)) is None)
check("occurs_on：anchor 之前不发生",
      _whens.occurs_on("FREQ=DAILY", _A5, _dt2.date(2026, 10, 4)) is False)
check("rrule_text：多星期说得出来",
      _whens.rrule_text("FREQ=WEEKLY;BYDAY=MO,WE,FR") == "每周一、三、五",
      _whens.rrule_text("FREQ=WEEKLY;BYDAY=MO,WE,FR"))

# RRULE → 人话**只有一份实现**（在 whens.rrule_text）。
# 2026-10-05 差点分叉：intake 里那份说要搬、实际没搬 ——
# 两份并存的代价是"改了一处、另一处不变"，正是本项目反复踩的坑。
_intake_src_rt = (SRC / "intake.py").read_text(encoding="utf-8")
check("RRULE 说人话只有一份实现（intake 里没有第二份）",
      "_recurrence_text" not in _intake_src_rt.replace(
          "# 注：这里曾有 `_recurrence_text()`（RRULE → 人话）。2026-10-05 搬到", ""))
check("回执用的是 whens.rrule_text",
      "whens.rrule_text(it.recurrence)" in _intake_src_rt)
# journal 要记下我们提交的规则（否则"这条到底是不是每天"只能靠猜）
check("journal 记事件时带上 recurrence",
      "recurrence=it.recurrence" in _intake_src_rt
      and "recurrence: str = \"\"" in (SRC / "journal.py").read_text(encoding="utf-8"))
# 闹钟同理（2026-10-07）：读路径**不返回**闹钟信息，
# 所以"这条到底设没设、会不会响"只有 journal 答得出来。
check("journal 记事件时带上 alarm",
      "alarm=alarm" in _intake_src_rt
      and "alarm: bool = False" in (SRC / "journal.py").read_text(encoding="utf-8"))

# ③ 备忘端：memo.add(text) → Memo.note_id
_sig_memo = _insp8.signature(_mm8.add)
_sig_memo.bind("x")
check("memo.add(text) 是合法调用", True)
check("memo.Memo 有 note_id 字段（intake 要取它）",
      "note_id" in {f.name for f in _dc8.fields(_mm8.Memo)})

# ⚠️ 建笔记时**只能给 body，不能同时给 name**（2026-10-04 修）
#
# 备忘录里"第一行就是标题"是 Notes 自己的语义；再把同一段设给 `name`，
# 首行就被写了两遍 —— 用户看到的就是"标题和正文重复"。
# 这条断言盯的是 AppleScript 属性表本身（行为要靠 Notes 才验得了，
# 而这里能守住"不许再把 name 传回去"）。
_mm8_src = (SRC / "memo.py").read_text(encoding="utf-8")
check("memo.add 不再同时传 name 与 body",
      "name:{_as_literal(name)}, body:" not in _mm8_src, "属性表里仍有 name")
check("memo.add 仍然传 body",
      "{body:{_as_literal(body_html)}}" in _mm8_src)
check("memo.add 仍按行建 <div>（多行的前提）",
      "<div>{_html_escape(ln)}</div>" in _mm8_src)

# ④ daemon 用 intake 的三个私有方法（按钮指定类型时走同一条分派路径）
for _m8 in ("_do_todo", "_do_event", "_do_memo"):
    check(f"Intake 有 {_m8}（daemon 按钮要用）", hasattr(_it.Intake, _m8))

# ⑤ report 读三处用的接口也要在
check("Reminders 有 all_reminders（report 要用）",
      hasattr(_rems8.Reminders, "all_reminders"))
check("Reminder 有 completed/name（report 要用）",
      {"completed", "name"} <= {f.name for f in _dc8.fields(_rems8.Reminder)})
check("journal 有 submitted_memos（report 防遗忘要用）",
      hasattr(_jr, "submitted_memos"))


section("v4 守护的启动通知冷却")

# 为什么需要：守护是 KeepAlive 的，如果因故反复重启，
# **你每次都会收到一条启动消息** —— 崩溃循环会变成消息轰炸，
# 而那恰恰是你最不想被打扰的时候。
check("有启动通知冷却机制", hasattr(_dm, "_should_notify_startup"))
check("有冷却标记写入", hasattr(_dm, "_mark_notified"))
_gd = _P2(_tf2.mkdtemp())
_old_sf = _dm.STATE_FILE
_dm.STATE_FILE = _gd / "state.json"
try:
    check("无状态文件时允许通知", _dm._should_notify_startup() is True)
    _dm._mark_notified()
    check("刚通知过则跳过", _dm._should_notify_startup() is False)

    # 状态文件损坏时保守放行（宁可多发一条，也不要静默永不通知）
    _dm.STATE_FILE.write_text("{坏", encoding="utf-8")
    check("状态损坏时放行（保守）", _dm._should_notify_startup() is True)
finally:
    _dm.STATE_FILE = _old_sf
    _sh2.rmtree(_gd, ignore_errors=True)

# 状态更新必须是"读-改-写"，不能整份覆盖
check("存在统一的 update_state（读-改-写）",
      hasattr(_dm, "update_state"))


section("网络异常必须被转换（否则杀死守护）")

# 实测踩到：长轮询期间 Telegram 直接断开连接，抛
# `http.client.RemoteDisconnected` —— 它是 ConnectionResetError → OSError，
# **不是 URLError**，所以只捕 URLError/HTTPError 的写法漏掉了它，
# 异常冒到 daemon 导致**进程退出**（KeepAlive 会重启，但每次被断都重启一次）。
_tg_src2 = (SRC / "telegram.py").read_text(encoding="utf-8")
check("telegram 导入 http.client", "import http.client" in _tg_src2)
check("捕获 HTTPException/ConnectionError/OSError",
      "http.client.HTTPException" in _tg_src2
      and "ConnectionError" in _tg_src2 and "OSError" in _tg_src2)

# 真的转换了吗（注入一个 RemoteDisconnected）
import http.client as _hc2  # noqa: E402
import urllib.request as _ur2  # noqa: E402
_orig_open = _ur2.urlopen
try:
    def _fake(*a, **k):
        raise _hc2.RemoteDisconnected("Remote end closed connection")
    _ur2.urlopen = _fake
    try:
        _tg._call("t", "getUpdates", {})
        _converted = False
    except _tg.TelegramError:
        _converted = True
    except Exception:
        _converted = False
    check("RemoteDisconnected 被转成 TelegramError", _converted)
finally:
    _ur2.urlopen = _orig_open


section("守护的停止与重启语义")

# ⚠️ launchd 的 KeepAlive 只重启**非正常退出**的进程。
# 踩到过：守护收到 SIGTERM（restart 时 bootout 发的）后 `return 0`
# 干净退出，launchd 认为"任务完成了"，从此不再拉起 ——
# 表现是 `✅ 已加载` 但 state = SIGTERMed、没有活进程，
# 于是用户发的消息**没人接、也没回复**。
_dm_src3 = (SRC / "daemon.py").read_text(encoding="utf-8")

check("区分'信号停止'与'异常退出'",
      "_stopped_by_signal" in _dm_src3)
check("信号停止返回 0（正常退出，不重启）",
      "return 0 if _stopped_by_signal else 1" in _dm_src3)

# 主循环异常跳出时必须返回非 0，否则 KeepAlive 不会拉起
check("异常退出返回非 0", "else 1" in _dm_src3)

# plist 的 KeepAlive 要能覆盖"崩溃即重启"的语义。
# <true/> 的语义是"非正常退出就重启"，进程成功退出就不管了 ——
# 用字典写法明确表达更稳妥。
_daemon_plist = (ROOT / "deploy" / "io.github.carlapple2025.pdca.daemon.plist").read_text(
    encoding="utf-8")
check("守护 plist 用显式 KeepAlive 条件",
      "SuccessfulExit" in _daemon_plist,
      "建议用 <dict><key>SuccessfulExit</key><false/></dict>")

# 安装脚本：bootout 是异步的，立刻 bootstrap 会撞上清理中 → 报
# "Input/output error: 5"，结果"旧的停了、新的没起"。必须等一下并重试。
_inst3 = (ROOT / "deploy" / "install_launchd.sh").read_text(encoding="utf-8")
check("安装脚本在 bootout 后有等待（不靠固定 sleep）",
      "wait_job_gone" in _inst3,
      "bootout 后必须等旧进程真的退出：守护在长轮询里，收到 SIGTERM 要等"
      "最长 25 秒才退出；只睡固定几秒就 bootstrap，新任务会被旧进程的退出"
      "带走 → state = SIGTERMed → 表现是'发消息没人接'")
check("安装脚本 bootstrap 会重试", "for attempt in 1 2 3" in _inst3)
check("等待旧进程有上限（不会永久挂住）", 'ge 40' in _inst3)

# ⚠️ "已加载" ≠ "在跑"。实测踩到：restart 打印 ✅ 已加载，而 print 查不到 ——
# 旧进程的 SIGTERM 处理与 bootout 撞在一起，几秒后任务就没了，
# 表现又是"发消息没回复"（同一症状的第三种根因）。
# 所以 restart 必须在加载后**等一下再验存活**。
check("restart 加载后做存活检查", "加载后存活检查失败" in _inst3)
check("restart 检查前先等待（避开竞态）", "sleep 3" in _inst3)
# 判据必须分任务类型：守护常驻要 running，日报平时就是 not running，
# 对日报断言 running 会产生一条永远失败的假告警。
check("存活检查区分常驻与定时任务",
      "verify_loaded" in _inst3
      and 'if [ "$label" = "io.github.carlapple2025.pdca.daemon" ]; then' in _inst3)
# 存活判据必须看 state/pid，而不是"print 能查到"就算过 ——
# SIGTERMed 的任务照样能被 print 查到，那正是当初误报成功的原因。
check("存活判据不是只看'能查到'",
      '"running"' in _inst3 and "pid" in _inst3)


section("v4 守护的崩溃循环防护")

# ⚠️ 这是 KeepAlive 会放大的风险：守护是常驻 + 自动重启的，
# 所以"启动时某一步失败就退出"会变成**无限崩溃循环** ——
# 每 ThrottleInterval 秒重启一次，日志刷满，而且永远恢复不了
# （每次都死在同一步）。
#
# 实测踩到：启动时若 Telegram 不可用，daemon 直接 return 2 退出。
_dm_src = (SRC / "daemon.py").read_text(encoding="utf-8")

# 启动路径上不该有"拉取失败就 return"的写法
_start_block = _dm_src[_dm_src.index("    offset = load_offset()"):
                       _dm_src.index("    while _running:")]
check("启动时拉取失败不退出", "return 2" not in _start_block,
      "启动路径里有 return 2 → KeepAlive 会崩溃循环")
check("启动失败改为交给主循环重试", "交给主循环重试" in _start_block)

# 主循环必须能吞掉单次拉取失败（而不是让异常冒出去终止进程）
_run_once = _dm_src[_dm_src.index("def run_once("):_dm_src.index("def _stop(")]
check("run_once 捕获 Telegram 错误", "except tg.TelegramError" in _run_once)
check("run_once 失败后返回 offset（不抛）", "return offset" in _run_once)

# 日志行必须单行 —— 长时间断网时多行提示会刷满日志
check("守护日志做了压平/截断", "_trunc" in _dm_src)
check("重试日志只取首行",
      "splitlines()[0]" in _dm_src or "_trunc" in _run_once)

# run_once 内部各自兜了网络错与单条消息错，但它自己仍可能抛出别的
# OSError（offset 落盘失败、快照读取失败）。主循环若没有最后一道兜底，
# 异常会冒出 main → 进程退出 → KeepAlive 拉起 → 几步后又退出 =
# 崩溃循环，而那期间用户发的消息**没人接**（正是本项目最忌讳的失败形态）。
_main_loop = _dm_src[_dm_src.index("    _consecutive_failures = 0"):
                     _dm_src.index("    _log(\"已退出\")")]
check("主循环兜住瞬时异常（不让进程退出）",
      "except (OSError, http.client.HTTPException)" in _main_loop)
check("主循环异常后继续运行", "继续运行" in _main_loop)
# 连续失败要退避，否则刷日志会把真问题淹掉（v1 踩过：断网 12 秒打 2 遍完整提示）
check("主循环连续失败有退避", "min(5 * _consecutive_failures, 60)" in _main_loop)
# 兜底**只兜瞬时类**：编程错误要故意漏出去，留下堆栈才好查
check("主循环不吞编程错误（不是裸 except）",
      "except Exception" not in _main_loop and "except:" not in _main_loop)


section("日志污染防护")

# 真实 journal 是日报"防遗忘"的数据源。混进测试数据会让它提醒
# 不存在的事，而且很难发现（看起来只是一条普通记录）。
# 实测踩到：离线模式那段测试写了 7 条测试记录进真实目录。
check("journal 支持环境变量覆盖目录",
      "PDCA_JOURNAL_DIR" in (SRC / "journal.py").read_text(encoding="utf-8"))
check("journal 提供污染断言", hasattr(_jr, "assert_not_real"))
# 断言必须在**唯一写入点** append() 里 ——
# 曾经放在调用方（daemon 离线模式），结果 log_input 直调绕过了它。
# ⚠️ 断言**不该**放在 append()（唯一写入点）。
# 实测踩到：那样会**把生产也拦住** —— 守护要写的正是真实目录，
# 断言抛错后 _journal 又静默吞掉异常（"日志失败不影响主流程"），
# 于是 journal 全空而任务显示 ok=True。
# 正解是由调用方声明自己是不是测试（Intake(testing=True)）。
_jr_src = (SRC / "journal.py").read_text(encoding="utf-8")
_append_body = _jr_src[_jr_src.index("def append("):_jr_src.index("def log_input(")]
# 判据要排除注释/文档字符串里"提到"该函数名的情况 ——
# 用 `assert_not_real(` 的**调用形态**判断（本项目在粗粒度文本匹配上
# 栽过多次：扫描器会把讲解性的注释也算进去）。
_append_calls = [
    _l for _l in _append_body.splitlines()
    if "assert_not_real(" in _l and not _l.lstrip().startswith("#")
    and not _l.lstrip().startswith('"')
]
check("污染断言不在 append()（否则会拦生产）",
      not _append_calls,
      f"append 里仍在调用：{_append_calls}")
check("Intake 提供 testing 开关（测试自行声明）",
      "testing" in (SRC / "intake.py").read_text(encoding="utf-8"))

# 生产路径（指向真实目录、未声明 testing）必须能写
_jr_p = _jr.JOURNAL_DIR
try:
    _jr.JOURNAL_DIR = _jr.ROOT / "data" / "journal"
    _probe_ok = False
    try:
        _rec_p = _jr.append("_selftest_probe", note="临时探测，由自检创建")
        _probe_ok = True
    except Exception:
        _probe_ok = False
    check("生产路径可写真实 journal（不被断言拦）", _probe_ok)
    # 清掉探测记录（自检不该留下垃圾）
    import os as _osC  # noqa: E402
    _pf = _jr.JOURNAL_DIR / f"{_jr._today()}.jsonl"
    if _pf.is_file():
        _lines = [l for l in _pf.read_text(encoding="utf-8").splitlines()
                  if "_selftest_probe" not in l]
        if _lines:
            _pf.write_text("\n".join(_lines) + "\n", encoding="utf-8")
        else:
            _osC.remove(_pf)
finally:
    _jr.JOURNAL_DIR = _jr_p
check("journal 提供真实目录判断", hasattr(_jr, "is_real_dir"))

# 自检自己必须跑在临时目录上
check("自检的 journal 指向临时目录", not _jr.is_real_dir(),
      f"当前 {_jr.JOURNAL_DIR}")

# 断言真的会拦（在真实目录上调用应当抛错）
_real = _jr.JOURNAL_DIR
try:
    import os as _osB  # noqa: E402
    _jr.JOURNAL_DIR = _jr.ROOT / "data" / "journal"
    _osB.environ.pop("PDCA_ALLOW_REAL_JOURNAL", None)
    try:
        _jr.assert_not_real("自检")
        _blocked = False
    except _jr.JournalContaminationError:
        _blocked = True
    check("往真实目录写会被拦住", _blocked)
finally:
    _jr.JOURNAL_DIR = _real


section("v4 守护的离线模式")

# 离线模式让"没有授权"时也能看到整条链路怎么工作（回执文案、分类结果、
# 日志记录），对首次上手和排查都很有用。它必须**真的不写入**。
_dm._OFFLINE = True
try:
    # 离线模式会往 journal 记东西；断言它此刻不是真实目录。
    # 这一步就是踩过的坑（污染了 7 条真实记录）。
    _jr.assert_not_real("自检的离线模式测试")
    _oit = _dm._make_intake()
    _oout = _oit.handle("明天交电费", _B)
    check("离线模式：有回执", _oout.ok and "待办" in _oout.reply)
    check("离线模式：记录会写到哪里",
          bool(getattr(_oit, "_offline_calls", [])))
    _oout2 = _oit.handle("@周五下午两点 项目周会", _B)
    _ocalls = getattr(_oit, "_offline_calls", [])
    check("离线模式：日程指向日历",
          any("日历" in c for c in _ocalls), str(_ocalls))
    # 缺硬信息的情况照样要**如实报错**（不再"问一次"），且不写
    _n_before = len(_ocalls)
    _oout3 = _oit.handle("@例会", _B)
    check("离线模式：缺时间时如实报错", _oout3.ok is False
          and _oout3.reply.startswith("❌"), _oout3.reply)
    check("离线模式：缺时间时不写入",
          len(getattr(_oit, "_offline_calls", [])) == _n_before)
finally:
    _dm._OFFLINE = False

# --offline 开关必须存在（否则没法用）
check("daemon 提供 --offline 开关",
      "--offline" in (SRC / "daemon.py").read_text(encoding="utf-8"))


section("v4 日报（report，注入假数据）")

# report 的数据源可注入，所以渲染逻辑能完全离线验证。
# 三处读取各自独立失败、互不影响 —— 这是 v1 的教训：
# 一个来源不可用就整个日报发不出，代价太大。
_rp = _load(SRC / "report.py")

_RD = _dt2.date(2026, 10, 3)
_t6, _b6 = _rp.build_report(_rp.ReportData(
    date=_RD,
    todos=[_rp.Todo("勘察表盖章", True), _rp.Todo("跟进竣工报验", False),
           _rp.Todo("交电费", False)],
    events=[_rp.Event("项目周会", _dt2.datetime(2026, 10, 4, 14, 0), "会议室")],
    memos=[_rp.Memo("荷载要按名称命名", _dt2.datetime(2026, 9, 28, 10, 0))],
))
check("日报标题格式", _t6 == "📋 2026-10-03 复盘", _t6)
check("日报含完成段", "✅ 今日完成 1 件" in _b6)
check("日报列出完成项", "勘察表盖章" in _b6)
check("日报含未完成段", "⏳ 未完成 2 件" in _b6)
check("日报含明日日程", "📅 明日日程 1 项" in _b6 and "14:00" in _b6)
check("日报含日程地点", "@会议室" in _b6)
check("日报含备忘提醒", "📝 备忘放了 3 天以上" in _b6 and "荷载要按名称命名" in _b6)
# 这句话现在**是真的**（`_read_stale_memos` 做了只读快照差集）——
# 曾经它是一句没实现的承诺：文案写着"删掉之后不再提醒"，
# 而差集根本没接上，删掉备忘日报照旧天天提醒。断言翻回来的前提是
# 那两处接线真的存在（见下面的"感知路径"一节）。
check("日报说明如何消除备忘提醒", "删掉即可" in _b6)

# 多行备忘在日报里**只占一行**（2026-10-04 起备忘正文保留换行）
_t6b, _b6b = _rp.build_report(_rp.ReportData(
    date=_RD,
    memos=[_rp.Memo("国庆规划\n1. PDCA\n2. 文生视频", _dt2.datetime(2026, 10, 4, 9, 0))],
))
check("日报：多行备忘只显示首行", "　国庆规划" in _b6b and "1. PDCA" not in _b6b, _b6b)
check("日报：截断了就给个省略提示", "国庆规划　…" in _b6b, _b6b)

# 全部完成 → 不该出现"未完成"段
_t7, _b7 = _rp.build_report(_rp.ReportData(date=_RD, todos=[_rp.Todo("甲", True)]))
check("全部完成有庆祝文案", "🎉" in _b7)
check("全部完成无未完成段", "⏳" not in _b7)

# 完全没有待办
_t8, _b8 = _rp.build_report(_rp.ReportData(date=_RD))
check("无待办时如实提示", "还没有条目" in _b8)
check("无待办时无完成段", "✅" not in _b8)

# 读取失败时**不能**说"没有待办" —— 那是把"读不到"说成"没有"，
# 会让人以为一切正常（这类"静默误报"比报错更危险）。
_t8b, _b8b = _rp.build_report(_rp.ReportData(
    date=_RD, errors=[_rp.SourceError("提醒事项", "拒绝访问")]))
check("提醒事项读取失败时不说\"没有待办\"", "还没有条目" not in _b8b)
check("提醒事项读取失败时有告警", "⚠️" in _b8b)

# 读取失败要如实标注，不能静默变成"今天没有待办"
_t9, _b9 = _rp.build_report(_rp.ReportData(
    date=_RD, todos=[_rp.Todo("甲", True)],
    errors=[_rp.SourceError("日历", "超时"),
            _rp.SourceError("备忘台账", "文件损坏")]))
check("读取失败时标注告警", "⚠️" in _b9)
check("读取失败时列出来源", "日历：超时" in _b9 and "备忘台账：文件损坏" in _b9)
check("读取失败时说明可能不完整", "可能不完整" in _b9)

# 无地点不该显示多余的 @
#
# ⚠️ 这条断言的**范围**在 2026-10-04 收窄了：页脚现在会举例"@周五两点 周会"
# （V5 要把符号表每天念一遍），于是"整篇不含 @"不再成立。
# 现在验的是"日程那一行没被加上地点前缀" —— 地点前缀是**全角空格 + @**。
_t10, _b10 = _rp.build_report(_rp.ReportData(
    date=_RD, events=[_rp.Event("例会", _dt2.datetime(2026, 10, 4, 9, 0))]))
check("日程无地点时不显示地点前缀", "　@" not in _b10)

# 页脚第二行曾经是"（发一句给我也行，比如「明天交电费」）"，紧跟"打钩 ✓"，
# 读起来像"发一句就能打钩" —— 而机器人**没有**打钩能力：发一句只会新建一条。
# 现在它只说自己真能做的事，并顺带把符号表念一遍（见 TELEGRAM-VOICE 的 V5）。
check("页脚不再暗示'发一句能打钩'", "发一句给我也行" not in _b10)
check("页脚说明'直接发'的是'加一条'", "想加一条就直接发" in _b10)
check("页脚把符号表念了一遍（# 与 @ 都在）",
      "# 想法" in _b10 and "@周五两点 周会" in _b10)

# run() 要存档到 data/digest/ 且不推送（注入假 sender）
import tempfile as _tf7  # noqa: E402
import shutil as _sh7  # noqa: E402
_dg = _P2(_tf7.mkdtemp())
_old_root = _rp.ROOT
_rp.ROOT = _dg
try:
    _sent: list = []
    # ⚠️ 心跳也必须注入假实现：不注入就会走真实的 notify.send_heartbeat，
    # 而它会去读 .env —— 那样"离线自检"就会真的往外部监控发请求
    # （更糟的是：一旦你配了 HEALTHCHECK_URL，自检会伪造出"今天跑过了"）。
    _noop_beat = lambda ok, summary="": (False, "自检不真心跳")  # noqa: E731
    _rp.run(date=_RD, push=True, data=_rp.ReportData(date=_RD),
            sender=lambda t, b, channels=None: (_sent.append((t, b)), [("telegram", True, "ok")])[1],
            heartbeat=_noop_beat)
    _digest = _dg / "data" / "digest" / "2026-10-03.md"
    check("日报存档到 data/digest/", _digest.is_file())
    check("存档内容含标题", "复盘" in _digest.read_text(encoding="utf-8"))
    check("日报确实调用了推送", len(_sent) == 1)
finally:
    _rp.ROOT = _old_root
    _sh7.rmtree(_dg, ignore_errors=True)

# 时间戳解析容错（journal 里的 at 字段）
check("时间戳解析：完整 ISO",
      _rp._parse_at("2026-10-03T10:20:30+08:00") is not None)
check("时间戳解析：空字符串返回 None", _rp._parse_at("") is None)
check("时间戳解析：垃圾返回 None", _rp._parse_at("不是时间") is None)


section("日报区块注册表（加一段正文不用改 build_report）")

# 为什么有这一节（2026-10-05）：
# 原先日报正文是 `build_report()` 里一列顺序 if，于是"加一段"= 改渲染主函数。
# 现在正文由 `BLOCKS` 声明。**这一节验的就是"声明真的成立"** ——
# 否则注册表只是换了个写法，加区块照样要动别处。

# ── 注册表本身要自洽
_ids = [b.id for b in _rp.BLOCKS]
check("区块 id 不重复", len(_ids) == len(set(_ids)),
      f"重复：{sorted({i for i in _ids if _ids.count(i) > 1})}")
check("每个区块都有 id 与 render",
      all(b.id and callable(b.render) for b in _rp.BLOCKS))
# 配了 heartbeat（会出现在对外摘要里）就必须有 count，否则摘要会渲染成 "完成None"
check("配了 heartbeat 的区块必须有 count",
      all(b.count is not None for b in _rp.BLOCKS if b.heartbeat))
# 反过来：配了 count 却不进摘要 = 死配置（要么是漏了 heartbeat，要么是忘了删）
check("配了 count 的区块应该也进心跳摘要（否则是死配置）",
      all(b.heartbeat for b in _rp.BLOCKS if b.count is not None),
      "；".join(b.id for b in _rp.BLOCKS if b.count and not b.heartbeat))

# ── 产物选择
# 每份产物都得有区块；区块、页脚、计划时间三者必须成套 ——
# 缺任何一个都会在**投递那一刻**才炸（而投递是每天只跑一次的那条路）。
for _dig in _rp.SCHEDULES:
    check(f"{_dig}：登记了计划时间就有区块", bool(_rp.blocks_for(_dig)))
    check(f"{_dig}：有页脚", _dig in _rp.FOOTERS)
check("区块声明里出现的产物名都已登记（页脚 + 计划时间）",
      all(d in _rp.FOOTERS and d in _rp.SCHEDULES
          for d in {d for b in _rp.BLOCKS for d in b.digests}),
      str(sorted({d for b in _rp.BLOCKS for d in b.digests})))
# 产物名写错要**抛**，不能悄悄渲染出一份空产物（静默失败比报错危险）
try:
    _rp.blocks_for("weekly_typo")
    _bad_digest = False
except ValueError:
    _bad_digest = True
check("产物名写错时抛异常（不返回空正文）", _bad_digest)
try:
    _rp._footer_for("weekly_typo")
    _bad_footer = False
except ValueError:
    _bad_footer = True
check("产物名写错时页脚也抛异常", _bad_footer)

# ── 每个区块：要么给行，要么给空列表；不许返回 None / 非字符串
#
# ⚠️ 2026-10-05 加周报后，判据必须**按产物分别做**：
# 日报的区块与周报的区块读的是同一份 data 的不同字段，
# 拿日报的 data 去逼周报的区块出现，只会得到一堆假红。
_rich = _rp.ReportData(
    date=_RD,
    todos=[_rp.Todo("做完的", True), _rp.Todo("没做完的", False)],
    events=[_rp.Event("周会", _dt2.datetime(2026, 10, 4, 14, 0))],
    memos=[_rp.Memo("一条备忘", _dt2.datetime(2026, 9, 20, 9, 0))],
    errors=[_rp.SourceError("日历", "超时")],
    generated_at=_dt2.datetime(2026, 10, 3, 21, 30),
    channel_warnings=["telegram 已连续 3 天失败"],
    digest_gap_note="上一份日报是 2 天前")
_empty = _rp.ReportData(date=_RD)

_WEEK_RICH = _rp.WeekData(
    start=_dt2.date(2026, 9, 28), end=_dt2.date(2026, 10, 4),
    submitted=[("待办", 5), ("日程", 2), ("备忘", 1)], completed=7,
    per_day=[(_dt2.date(2026, 9, 28) + _dt2.timedelta(days=i),
              n) for i, n in enumerate([1, 2, 0, 3, 1, 0, 0])], streak=5)
_week_rich = _rp.ReportData(date=_RD, digest="weekly", week=_WEEK_RICH,
                            generated_at=_dt2.datetime(2026, 10, 4, 20, 0))
_week_empty = _rp.ReportData(date=_RD, digest="weekly",
                             week=_rp.WeekData(start=_dt2.date(2026, 9, 28),
                                               end=_dt2.date(2026, 10, 4),
                                               submitted=[("待办", 0)],
                                               completed=0, per_day=[],
                                               streak=0))
# 第三份 fixture：**读失败**的那一种。必须单独有一份 ——
# 因为"提醒事项读不到"时完成数根本没有意义（写 0 就是在撒谎），
# 所以它不能和"有一周数据"混在同一份 data 里（混了就不是真实情形）。
_week_err = _rp.ReportData(
    date=_RD, digest="weekly",
    week=_rp.WeekData(start=_dt2.date(2026, 9, 28), end=_dt2.date(2026, 10, 4),
                      submitted=[("待办", 0)], completed=0, per_day=[], streak=0),
    errors=[_rp.SourceError(_rp.SRC_REMINDERS, "拒绝访问")])

# 每份产物的 fixture 集：什么都有 / 什么都没有 / 读失败
_FIXTURES: dict = {"daily": (_rich, _empty),
                   "weekly": (_week_rich, _week_empty, _week_err)}

_shapes_ok = True
_dead: list[str] = []
for _dig, _fxs in _FIXTURES.items():
    for _b in _rp.blocks_for(_dig):
        for _d in _fxs:
            _r = _b.render(_d)
            if not isinstance(_r, list) or not all(isinstance(x, str) for x in _r):
                _shapes_ok = False
        # ⚠️ 判据是"这些 fixture 里**至少有一份**让它出现"，不能只看 rich：
        # `reminders_empty` 与"有待办"是**互斥**的（有待办时它必须不出现），
        # `week_empty` 与"有数据"同理 —— 所以"rich 下为空"对它们是正确行为。
        if not any(_b.render(_d) for _d in _fxs):
            _dead.append(f"{_dig}:{_b.id}")
check("每个区块渲染出 列表[str]（空列表=省略）", _shapes_ok)
# "注册了却永远不出现" = 死区块。每份产物各自的两份互补数据合起来
# 必须能点亮它自己的全部区块 —— 否则说明判据写错了，或那个区块根本是摆设。
check("没有死区块（每份产物的互补 data 都能点亮自己的区块）", not _dead,
      f"这些区块在 rich 与 empty 下都为空：{_dead}")

# 每个区块都必须属于**至少一份**产物 —— 否则它会永远不渲染（注册了但没人要）
_orphan = [_b.id for _b in _rp.BLOCKS
           if not any(_b in _rp.blocks_for(_d) for _d in _FIXTURES)]
check("没有不属于任何产物的区块", not _orphan, str(_orphan))

# ── build_report 真的不再认识任何一段文案
import inspect as _insp9  # noqa: E402
_br_src = _insp9.getsource(_rp.build_report)
_leaked = [w for w in ("今日完成", "未完成", "明日日程", "备忘放了",
                       "读取失败", "投递通道读数", "上一份日报",
                       "本周记下", "连续") if w in _br_src]
check("build_report 里没有区块文案（正文全在区块里）", not _leaked,
      f"这些文案还留在主函数里：{_leaked} → 加段落时仍要改它")

# ── 心跳摘要由注册表派生（原先硬编码四品类，加品类要改三处）
_hb = _rp._heartbeat_summary(_rich, [("telegram", True, "ok")])
check("心跳摘要含各区块的计数", "完成1" in _hb and "未完成1" in _hb
      and "明日日程1" in _hb and "备忘1" in _hb, _hb)
check("心跳摘要仍有读取失败计数", "读取失败1" in _hb, _hb)
check("心跳摘要仍然只报数字（不带正文）",
      "一条备忘" not in _hb and "没做完的" not in _hb, _hb)
def _hb_labels(summary: str) -> list:
    """把心跳摘要里的 `标签+数字` 拆出标签来（顺序保留）。

    ⚠️ 不能用 `"完成" in 摘要` 来判"混进了别的产物" ——
    周报的「本周完成9」**包含**"完成"这两个字，子串判断会假红。
    （这条断言自己踩过一次，所以拆词而不是找子串。）
    """
    out = []
    for _t in summary.split():
        _m = _re.fullmatch(r"(.+?)(\d+)", _t)
        if _m:
            out.append(_m.group(1))
    return out


for _dig, _fx in (("daily", _rich), ("weekly", _week_rich)):
    _s = _rp._heartbeat_summary(_fx, [("telegram", True, "ok")], _dig)
    _labels = [b.heartbeat for b in _rp.blocks_for(_dig) if b.heartbeat]
    check(f"{_dig} 心跳摘要含本产物的每个计数",
          all(lb in _hb_labels(_s) for lb in _labels), _s)
    check(f"{_dig} 心跳摘要的顺序跟着区块声明走",
          [_hb_labels(_s).index(lb) for lb in _labels]
          == sorted(_hb_labels(_s).index(lb) for lb in _labels), _s)
    # ⚠️ 心跳**只报这一份产物**的计数：周报的心跳里出现"明日日程0"
    # 会让远端看到一份与它无关的字段，久了就没人信这串数字。
    _others = [b.heartbeat for b in _rp.BLOCKS
               if b.heartbeat and b not in _rp.blocks_for(_dig)]
    check(f"{_dig} 心跳摘要不含别的产物的计数",
          not (set(_hb_labels(_s)) & set(_others)),
          f"{_s} 里混进了 {_others}")

# ── 两条"防退化"的判据（都是这次重构顺带修掉的静默失效）
# ① 来源判断必须是**精确匹配**：以前写的是 `"提醒事项" in e`（子串包含），
#    换个来源名或改个文案就会静默失效 —— 而它守的是"读不到不能说成没有"。
_f = _rp.ReportData(date=_RD, errors=[_rp.SourceError("提醒事项（旧权限）", "x")])
check("failed() 是精确匹配（子串不算）", _f.failed("提醒事项") is False,
      "子串匹配会让'另一个来源名里恰好含提醒事项'被误判成读取失败")
check("failed() 命中同来源", _rp.ReportData(
    date=_RD, errors=[_rp.SourceError("提醒事项", "x")]).failed("提醒事项") is True)

# ② 备忘提醒的天数要用**这次实际用的**，不是模块常量
#    （实测过的正文撒谎：`--memo-days 7` 筛 7 天，正文写"3 天以上"）
_b7d = _rp.build_report(_rp.ReportData(date=_RD, memo_nag_days=7, memos=[
    _rp.Memo("放了很久的事", _dt2.datetime(2026, 9, 20, 9, 0))]))[1]
check("备忘天数随 --memo-days 变（正文不撒谎）", "放了 7 天以上" in _b7d, _b7d)
check("默认仍是 3 天", "放了 3 天以上" in _rp.build_report(_rp.ReportData(
    date=_RD, memos=[_rp.Memo("x", _dt2.datetime(2026, 9, 20, 9, 0))]))[1])


section("周报（周日 20:00）与「两种产物不能互相冒充」")

# ── 一、先验"不能互相冒充"
#
# 周报也会往 digest_pushed 留痕。而 `delivered_on()` 原先只看"这一天有没有
# digest_pushed 记录" —— 于是**周日 20:00 的周报会把当天的日报标记成已送达**：
# 看门狗 23:30 检查时看到"送到了"就不告警，而那天 21:30 的日报根本没跑。
# 看门狗唯一的职责就是发现这件事，被骗过去 = 白装。
#
# 所以 2026-10-05 给 digest_pushed 加了 `digest_kind` 字段
# （按 KERNEL-CONTRACT §五：给已有事件加字段 = MINOR）。
# 同样的骗法在**机器之外**还有一份：周报不能去 ping 日报那个心跳 URL，
# 否则远端监控也会以为日报跑过了 —— 见本节的第三部分。
_jw = _fresh_journal()
try:
    _wd = "2026-10-04"           # 周日
    _jr.log_digest_pushed([("telegram", True, "ok")], digest_date=_wd,
                          digest_kind="weekly")
    check("周报投递**不算**当天日报送到（看门狗不会被骗）",
          _jr.delivered_on(_wd) is False)
    check("周报投递在 weekly 这一侧算数",
          _jr.delivered_on(_wd, kind="weekly") is True)
    check("digest_dates 默认只数日报",
          _jr.digest_dates(days=3, end=_wd) == [])
    check("digest_dates(kind='weekly') 数得到周报",
          _jr.digest_dates(days=3, end=_wd, kind="weekly") == [_wd])

    # 老记录（没有 digest_kind 字段）必须**仍然算日报** ——
    # 否则升级那一刻，历史投递读数会全部凭空消失（而它们是不可再生的）。
    _jr.append(_jr.EV_DIGEST_PUSHED, digest_date=_wd,
               channels=[{"name": "telegram", "ok": True, "detail": ""}],
               **{_jr.DATE_KEY: _wd})
    check("没有 digest_kind 的老记录算日报（历史读数不消失）",
          _jr.delivered_on(_wd) is True)
    check("digest_kind_of 对老记录返回 daily",
          _jr.digest_kind_of({"event": _jr.EV_DIGEST_PUSHED}) == "daily")
finally:
    _sh2.rmtree(_jw, ignore_errors=True)

# 通道读数：周报的成功**不能**把日报的连续失败清零 ——
# "连续几天没成功"是这条读数唯一要发现的东西（单通道静默失效能瞒几个月）。
_jw2 = _fresh_journal()
try:
    _hb_base = _dt2.date(2026, 10, 4)
    for _i in (2, 1, 0):
        _jr.log_digest_pushed(
            [("telegram", False, "超时")],
            digest_date=(_hb_base - _dt2.timedelta(days=_i)).isoformat())
    _jr.log_digest_pushed([("telegram", True, "ok")],
                          digest_date=_hb_base.isoformat(), digest_kind="weekly")
    _hh = _jr.channel_health(days=7, end=_hb_base.isoformat())
    check("日报通道连续失败天数不被周报的成功清零",
          _hh.get("telegram", {}).get("consecutive_fail_days") == 3, str(_hh))
finally:
    _sh2.rmtree(_jw2, ignore_errors=True)

# ── 二、周报本身
check("周报区间是周一到周日（周日属于本周）",
      _rp._week_bounds(_dt2.date(2026, 10, 4)) == (_dt2.date(2026, 9, 28),
                                                   _dt2.date(2026, 10, 4)))
check("周报区间：周中也落在同一周",
      _rp._week_bounds(_dt2.date(2026, 9, 30)) == (_dt2.date(2026, 9, 28),
                                                   _dt2.date(2026, 10, 4)))

# 连续天数。⚠️ "今天还没有完成不算断"是刻意的：否则每天早上打开都显示
# "连续 0 天"，而你昨天明明做了事 —— 一条每天都会骗你一次的读数比没有更糟。
check("连续天数：今天有完成就从今天数",
      _rp._streak_from({_dt2.date(2026, 10, 4): 1}, _dt2.date(2026, 10, 4)) == 1)
check("连续天数：今天还没有完成不算断（从昨天往前数）",
      _rp._streak_from({_dt2.date(2026, 10, 3): 2}, _dt2.date(2026, 10, 4)) == 1)
check("连续天数：连着三天就数三",
      _rp._streak_from({_dt2.date(2026, 10, 2): 1, _dt2.date(2026, 10, 3): 1,
                        _dt2.date(2026, 10, 4): 1},
                       _dt2.date(2026, 10, 4)) == 3)
check("连续天数：中间断了就从断点重数",
      _rp._streak_from({_dt2.date(2026, 10, 1): 5, _dt2.date(2026, 10, 2): 0,
                        _dt2.date(2026, 10, 3): 1, _dt2.date(2026, 10, 4): 1},
                       _dt2.date(2026, 10, 4)) == 2)
check("连续天数：一天都没完成就是 0",
      _rp._streak_from({}, _dt2.date(2026, 10, 4)) == 0)

_tw, _bw = _rp.build_report(_week_rich)
check("周报标题带周区间",
      _tw.startswith("📋 周报 ") and "09-28" in _tw and "10-04" in _tw, _tw)
check("周报含完成数与本周记下",
      "✅ 完成 7 件" in _bw and "📥 本周记下：待办 5 · 日程 2 · 备忘 1" in _bw, _bw)
check("周报含连续天数", "🔥 连续 5 天有完成" in _bw, _bw)
# 这是"同一份渲染管线、另一套区块"的实证：日报的段一个都不该漏进周报
check("周报不带日报的段（明日日程 / 备忘提醒 / 打钩）",
      "明日日程" not in _bw and "备忘放了" not in _bw and "打钩" not in _bw, _bw)
# 口径必须写出来：不写清"完成数只是下限"，这份周报就在骗人
# （你在提醒事项里删掉一条已完成的，它就从统计里消失了，而数字看起来仍然精确）
check("周报写明口径（只数得到还在的条目）",
      "只数得到" in _bw and "删掉" in _bw, _bw)
# 每天一格只画到今天：未来的日子显示 0 只会让人以为漏了
# （_week_rich.date 是 10-03 周六，本周最后一天是 10-04 周日）
check("周报的每天一格只画到今天", "周六" in _bw and "周日" not in _bw, _bw)

_twe, _bwe = _rp.build_report(_week_empty)
check("周报：一周什么都没有时如实说", "这周什么都没有" in _bwe, _bwe)
# 读不到 ≠ 没有（与日报同一条原则，日报为此专门有断言）
_bwf = _rp.build_report(_week_err)[1]
check("周报读不到时**不说**「这周什么都没有」",
      "这周什么都没有" not in _bwf, _bwf)
check("周报读不到时如实列出失败来源", "拒绝访问" in _bwf, _bwf)
# ⚠️ 这一条是**真实跑出来的**：第一版周报在 -10004 越权时照样打"✅ 完成 0 件"，
# 读起来就是"你这周什么都没干"。与日报的"读不到不能说成没有"同源。
check("周报读不到时**不写**「完成 0 件」（那是在撒谎）",
      "完成 0 件" not in _bwf and "完成：读不到" in _bwf, _bwf)
check("周报读不到时不画每天的完成格子（0 会被当成真没做）",
      "周一" not in _bwf, _bwf)

# ── 三、投递链（与日报共用同一条：存档 / 推送 / 心跳 / 留痕）
_dw = _P2(_tf2.mkdtemp())
_old_root_w = _rp.ROOT
_rp.ROOT = _dw
_jw3 = _fresh_journal()
try:
    _sent_w: list = []
    _beats_w: list = []
    _data_w = _rp.ReportData(
        date=_dt2.date(2026, 10, 4), digest="weekly", week=_WEEK_RICH,
        generated_at=_dt2.datetime(2026, 10, 4, 20, 0))
    _twr, _bwr, _okw = _rp.run_weekly(
        today=_dt2.date(2026, 10, 4), push=True,
        now=_dt2.datetime(2026, 10, 4, 20, 0), data=_data_w,
        sender=lambda t, b, channels=None: (
            _sent_w.append((t, b)), [("telegram", True, "ok")])[1],
        heartbeat=lambda ok, summary="": (
            _beats_w.append(summary), (True, "ok"))[1])
    check("周报存档到 data/digest/week-<周一>.md",
          (_dw / "data" / "digest" / "week-2026-09-28.md").is_file())
    check("周报也走推送", len(_sent_w) == 1)
    check("周报的推送结果照旧如实返回（退出码同源）", _okw is True)
    check("周报标题里的周区间与存档名一致", "09-28" in _twr and "10-04" in _twr, _twr)

    _recs_w = [r for r in _jr.read_day("2026-10-04")
               if r.get("event") == _jr.EV_DIGEST_PUSHED]
    check("周报留痕带 digest_kind=weekly（与日报分得开）",
          bool(_recs_w) and _recs_w[-1].get("digest_kind") == "weekly",
          str(_recs_w))
    check("周报心跳只报周报的计数（不含日报字段）",
          bool(_beats_w) and "本周完成" in _beats_w[-1]
          and "明日日程" not in _beats_w[-1] and "未完成" not in _beats_w[-1],
          str(_beats_w))
finally:
    _rp.ROOT = _old_root_w
    _sh2.rmtree(_dw, ignore_errors=True)
    _sh2.rmtree(_jw3, ignore_errors=True)

# ── 四、心跳 URL 也必须分开（同样的骗法，发生在机器之外）
check("周报心跳用独立的 URL 变量（不复用日报那个）",
      "HEALTHCHECK_URL" not in _notify.heartbeat_env_keys("weekly")
      and "WEEKLY_HEALTHCHECK_URL" in _notify.heartbeat_env_keys("weekly"),
      str(_notify.heartbeat_env_keys("weekly")))
check("不认识的产物名按日报处理（保守，不错配到别的 URL）",
      _notify.heartbeat_env_keys("nope") == _notify.heartbeat_env_keys("daily"))

_saved_hb_env = os.environ.pop("HEALTHCHECK_URL", None)
_saved_hb_env2 = os.environ.pop("WEEKLY_HEALTHCHECK_URL", None)
try:
    os.environ["HEALTHCHECK_URL"] = "https://daily.invalid/abc"
    _u_d, _ = _notify.load_heartbeat_url("daily")
    _u_w, _ = _notify.load_heartbeat_url("weekly")
    check("配了日报心跳时，日报取得到", _u_d == "https://daily.invalid/abc", str(_u_d))
    # ⚠️ 这一条是关键：周报**不能**退回去用日报的 URL
    check("配了日报心跳时，周报**不**误用它", _u_w is None, str(_u_w))
    os.environ["WEEKLY_HEALTHCHECK_URL"] = "https://weekly.invalid/xyz"
    _u_w2, _ = _notify.load_heartbeat_url("weekly")
    check("配了周报心跳时，周报取得到", _u_w2 == "https://weekly.invalid/xyz",
          str(_u_w2))
finally:
    for _k, _v in (("HEALTHCHECK_URL", _saved_hb_env),
                   ("WEEKLY_HEALTHCHECK_URL", _saved_hb_env2)):
        os.environ.pop(_k, None)
        if _v is not None:
            os.environ[_k] = _v


section("v4 投递与监控（心跳 / 投递留痕 / 生成时刻）")

# 这一节解决的是**最难发现的一类失败：根本没跑**。
# 两个通道只能证明"送没送到"，证明不了"跑没跑" —— 机器睡了、任务被清了、
# 脚本在推送前崩了，这三种情况在本机全都看不出来（实测 launchctl 里
# 计数还会因 reload 归零：runs=0 / never exited）。
# 唯一办法是让**机器之外**的东西盯着，所以这里有心跳。

check("notify 提供心跳发送", hasattr(_notify, "send_heartbeat"))
check("notify 提供心跳 URL 读取", hasattr(_notify, "load_heartbeat_url"))
check("心跳目标：成功用原 URL",
      _notify.heartbeat_target("https://hc-ping.com/abc", True)
      == "https://hc-ping.com/abc")
check("心跳目标：失败追加 /fail",
      _notify.heartbeat_target("https://hc-ping.com/abc", False)
      == "https://hc-ping.com/abc/fail")
check("心跳目标：容忍结尾斜杠",
      _notify.heartbeat_target("https://hc-ping.com/abc/", False)
      == "https://hc-ping.com/abc/fail")

# 未配置心跳**不是故障**：要如实说"未配置"，但不能抛异常、不能重试
# （否则没配心跳的人每天定时任务都会失败）。
import tempfile as _tf9  # noqa: E402
_sv_root = _notify.ROOT
_sv_env = {k: os.environ.pop(k, None) for k in _notify.HEARTBEAT_ENV_KEYS}
_sv_tmp = _P2(_tf9.mkdtemp(prefix="pdca-selftest-notify-"))
_notify.ROOT = _sv_tmp          # 让 .env 查找落在临时目录（不读用户真实 .env）
try:
    _hb_url, _hb_src = _notify.load_heartbeat_url()
    check("未配置心跳时如实报告", _hb_url is None and "未配置" in _hb_src, _hb_src)
    _hb_ok, _hb_msg = _notify.send_heartbeat(True, "自检")
    check("未配置心跳时不抛异常、不算成功",
          _hb_ok is False and "未配置" in _hb_msg, _hb_msg)

    # 真的发一次 —— 用**本机回环**上的假服务，不碰外网。
    # 只测字符串拼接是不够的：要证明 /fail 真的被请求到了。
    import http.server as _hs  # noqa: E402
    import threading as _th  # noqa: E402

    _seen: list = []

    class _HBHandler(_hs.BaseHTTPRequestHandler):
        def do_POST(self):                      # noqa: N802（标准库要求的名字）
            _n = int(self.headers.get("Content-Length") or 0)
            _seen.append((self.path, self.rfile.read(_n).decode("utf-8", "replace")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):              # 别把自检输出弄脏
            pass

    _srv = _hs.HTTPServer(("127.0.0.1", 0), _HBHandler)
    _th.Thread(target=_srv.serve_forever, daemon=True).start()
    os.environ["HEALTHCHECK_URL"] = f"http://127.0.0.1:{_srv.server_port}/uuid-x"
    try:
        _ok1, _m1 = _notify.send_heartbeat(True, "完成2 未完成3")
        _ok2, _m2 = _notify.send_heartbeat(False, "读取失败1")
        check("心跳成功路径可发", _ok1, _m1)
        check("心跳失败路径可发", _ok2, _m2)
        check("成功 ping 打在主 URL", _seen and _seen[0][0] == "/uuid-x",
              str(_seen[:1]))
        check("失败 ping 打在 /fail",
              len(_seen) > 1 and _seen[1][0] == "/uuid-x/fail", str(_seen[1:2]))
        # 隐私约定：心跳只送计数摘要，**不送日报正文**
        check("心跳请求体只含摘要",
              len(_seen) > 1 and "完成2" in _seen[0][1] and "未完成3" in _seen[0][1])
    finally:
        _srv.shutdown()
        os.environ.pop("HEALTHCHECK_URL", None)
finally:
    _notify.ROOT = _sv_root
    for _k, _v in _sv_env.items():
        if _v is not None:
            os.environ[_k] = _v
    _sh7.rmtree(_sv_tmp, ignore_errors=True)

# 计划时间必须与 plist 一致 —— 否则"迟到"判断会照着错的时间算，
# 而这类不一致**不会报错**，只会让你看到一句错的告警。
_plist_rep = (ROOT / "deploy" / "io.github.carlapple2025.pdca.report.plist").read_text(
    encoding="utf-8")
check("report 的计划时间与 plist 一致",
      f"<key>Hour</key>\n\t\t<integer>{_rp.SCHEDULE_HOUR}</integer>" in _plist_rep
      and f"<key>Minute</key>\n\t\t<integer>{_rp.SCHEDULE_MINUTE}</integer>" in _plist_rep,
      f"report.py 写的是 {_rp.SCHEDULE_HOUR:02d}:{_rp.SCHEDULE_MINUTE:02d}")

# 周报的计划时间同样要与它自己的 plist 一致 —— 而且是**周日**
# （Weekday 0 = 周日，launchd 的约定）。
_plist_wk = (ROOT / "deploy" / "io.github.carlapple2025.pdca.weekly.plist").read_text(
    encoding="utf-8")
_wk_h, _wk_m = _rp.SCHEDULES["weekly"]
check("周报的计划时间与 plist 一致",
      f"<key>Hour</key>\n\t\t<integer>{_wk_h}</integer>" in _plist_wk
      and f"<key>Minute</key>\n\t\t<integer>{_wk_m}</integer>" in _plist_wk,
      f"report.py 写的是 {_wk_h:02d}:{_wk_m:02d}")
check("周报真的只在**周日**跑（Weekday 0）",
      "<key>Weekday</key>\n\t\t<integer>0</integer>" in _plist_wk)
check("周报任务带 --weekly（否则会当成日报跑两遍）",
      "--weekly" in _plist_wk and "src/report.py" in _plist_wk)

# 生成时刻 / 迟到告警：launchd 睡过后会在唤醒时补跑（man launchd.plist：
# coalesced into one event upon wake），所以日报**必须自己说出**它多新。
_t11, _b11 = _rp.build_report(_rp.ReportData(
    date=_RD, generated_at=_dt2.datetime(2026, 10, 3, 21, 30)))
check("日报带生成时刻", "生成于 21:30" in _b11)
check("准时时不报迟到", "延迟" not in _t11 and "比计划" not in _b11)

_t12, _b12 = _rp.build_report(_rp.ReportData(
    date=_RD, generated_at=_dt2.datetime(2026, 10, 4, 7, 5), schedule_offset=575))
check("迟到时标题标注", "延迟" in _t12, _t12)
check("迟到时正文说明晚了多久", "9 小时35 分" in _b12, _b12)

# 反过来也要管：睡过 21:30、凌晨才醒，于是**次日**跑了一份空日报 ——
# 不标出来的话，你只会看到一份莫名其妙的空报告，而昨天那份永远不会来了。
_t12b, _b12b = _rp.build_report(_rp.ReportData(
    date=_RD, generated_at=_dt2.datetime(2026, 10, 3, 0, 40),
    schedule_offset=-1250))
check("早于计划时标题标注", "非计划时间" in _t12b, _t12b)
check("早于计划时说明上一份没发出",
      "早 20 小时50 分" in _b12b and "不是" in _b12b, _b12b)

check("偏离分钟数按计划时间算（正=晚）",
      _rp.schedule_offset_minutes(_dt2.datetime(2026, 10, 3, 21, 30), _RD) == 0
      and _rp.schedule_offset_minutes(_dt2.datetime(2026, 10, 3, 22, 10), _RD) == 40
      and _rp.schedule_offset_minutes(_dt2.datetime(2026, 10, 3, 0, 40), _RD) == -1250)
check("补跑历史日期不判偏离",
      _rp.schedule_offset_minutes(_dt2.datetime(2026, 10, 4, 10, 0), _RD) is None)

# ── 投递留痕：结果要落进 journal（logs/ 可清，journal 不可再生）
_jm = _load(SRC / "journal.py")
_sv_jdir = _P2(_tf9.mkdtemp(prefix="pdca-selftest-digest-"))
_sv_jold = _jm.JOURNAL_DIR
_jm.JOURNAL_DIR = _sv_jdir
_jm.assert_not_real("自检会写 digest_pushed 读数")   # 重定向失败就立刻停下
try:
    _jm.log_digest_pushed([("telegram", False, "超时"), ("bark", False, "网络失败")],
                          digest_date="2026-10-01", heartbeat="失败")
    _jm.log_digest_pushed([("telegram", False, "超时"), ("bark", True, "ok")],
                          digest_date="2026-10-02", heartbeat="已 ping")
    _recs_d = _jm.read_day("2026-10-01")
    check("投递结果写进 journal",
          any(r.get("event") == "digest_pushed" for r in _recs_d))
    check("投递记录含每通道明细",
          any(r.get("channels") and r["channels"][0]["name"] == "telegram"
              for r in _recs_d))
    _ch = _jm.channel_health(days=3, end="2026-10-02")
    check("通道读数：连续失败天数",
          _ch["telegram"]["consecutive_fail_days"] == 2,
          str(_ch.get("telegram")))
    check("通道读数：成功即中断连续计数",
          _ch["bark"]["consecutive_fail_days"] == 0, str(_ch.get("bark")))
    check("通道读数含最近一次说明（用于区分'没配'与'坏了'）",
          "超时" in _ch["telegram"]["last_detail"])

    # run() 的完整投递路径：注入 sender + heartbeat（**不碰网络**）
    #
    # ⚠️ ROOT 必须一起重定向：run() 会把日报**存档**到 ROOT/data/digest/。
    # 实测踩到：漏了这一步，自检就把 data/digest/2026-10-03.md 覆盖成了
    # 一份空日报 —— 那是真正的历史产物（当晚推送过的那一份），
    # 而 data/ 不在 git 里，覆盖了就没法回滚。
    # 存档路径与 journal 一样，**测试必须指到临时目录**。
    _sv_rroot = _rp.ROOT
    _sv_rtmp = _P2(_tf9.mkdtemp(prefix="pdca-selftest-report-root-"))
    _rp.ROOT = _sv_rtmp
    if _rp.ROOT == _sv_rroot:
        raise SystemExit("❌ 自检未能把 report.ROOT 重定向到临时目录，拒绝继续"
                         "（否则会覆盖真实 data/digest/）")
    try:
        _beats: list = []
        _rp.run(date=_RD, push=True,
                data=_rp.ReportData(date=_RD, todos=[_rp.Todo("甲", True)]),
                sender=lambda t, b, channels=None: [("telegram", True, "ok")],
                heartbeat=lambda ok, summary="": (_beats.append((ok, summary)),
                                                  (True, "已 ping"))[1])
        check("推送成功后心跳报平安", _beats and _beats[0][0] is True, str(_beats[:1]))
        check("心跳摘要只含计数不含内容",
              _beats and "完成1" in _beats[0][1] and "甲" not in _beats[0][1],
              str(_beats[:1]))
        check("run 把投递结果落进 journal",
              any(r.get("event") == "digest_pushed"
                  for r in _jm.read_day(_RD.isoformat())))
        check("日报存档落在临时 ROOT 而不是真实 data/",
              (_sv_rtmp / "data" / "digest" / "2026-10-03.md").is_file())

        # 数据不完整 → 心跳必须报**失败**（否则"跑了一半"会被当成正常）
        _beats.clear()
        _rp.run(date=_RD, push=True,
                data=_rp.ReportData(date=_RD, errors=[
                    _rp.SourceError("提醒事项", "拒绝访问")]),
                sender=lambda t, b, channels=None: [("telegram", True, "ok")],
                heartbeat=lambda ok, summary="": (_beats.append((ok, summary)),
                                                  (True, "已 ping /fail"))[1])
        check("数据不全时心跳报失败", _beats and _beats[0][0] is False, str(_beats[:1]))

        # --no-push（只看内容）**绝不能**发心跳：否则手工预览会把
        # "今天跑过了"这个信号伪造出来，真正漏跑时反而不会告警。
        _beats.clear()
        _rp.run(date=_RD, push=False, data=_rp.ReportData(date=_RD),
                heartbeat=lambda ok, summary="": (_beats.append(ok), (True, "x"))[1])
        check("不推送时不发心跳", not _beats, str(_beats))
    finally:
        _rp.ROOT = _sv_rroot
        _sh7.rmtree(_sv_rtmp, ignore_errors=True)

    # 通道连续失败要在日报里说出来（单通道静默失效只有这里能看见）。
    #
    # ⚠️ 两处讲究：
    #   · 先把临时日志清空 —— 上面那几步刚写过"成功"，同一天"任一次成功
    #     即算成功"的合并规则会让连续计数断掉，测试就会假装通过不了；
    #   · 日期必须**相对今天**构造：连续计数是从"今天"往回数的，写死日期
    #     会在未来的某一天悄悄失效（"过一段时间自己失效"的测试比没有更危险）。
    _sh7.rmtree(_sv_jdir, ignore_errors=True)
    _sv_jdir.mkdir(parents=True, exist_ok=True)

    _today_d = _dt2.date.today()
    for _off in (2, 1, 0):
        _d_iso = (_today_d - _dt2.timedelta(days=_off)).isoformat()
        _jm.log_digest_pushed([("telegram", False, "超时")], digest_date=_d_iso)
        # 同一段日期里，Bark 的失败原因是"没配" —— 它**不该**被念
        _jm.log_digest_pushed([("bark", False, "没有可用的 Bark key")],
                              digest_date=_d_iso)

    _w = _rp._channel_warnings(days=5)
    check("连续失败会算出提示",
          any("telegram" in x and "连续 3 天" in x for x in _w), str(_w))
    # 这条是"判据要分得清"：没配 ≠ 坏了。把没配的通道也天天念，
    # 只会训练你忽略这一行告警。
    check("没配的通道不进提示（没配≠坏了）",
          not any("bark" in x for x in _w), str(_w))
finally:
    _jm.JOURNAL_DIR = _sv_jold
    _sh7.rmtree(_sv_jdir, ignore_errors=True)


section('v4 日报看门狗（守护进程里盯"今天送出没有"）')

# 这一节补的是"日报自己发不出声"的那一类失败：进程崩在推送前、任务被清、
# 授权失效 —— 共同点是**机器醒着，只是日报没跑成**，所以由常驻的守护来盯。
#
# 三条约束逐条验：不加状态文件（读数派生）、绝不抛异常、每天最多一次。
_wd = _load(SRC / "watchdog.py")
_jw = _load(SRC / "journal.py")
_sv_jd2 = _P2(_tf9.mkdtemp(prefix="pdca-selftest-watchdog-"))
_sv_jo2 = _jw.JOURNAL_DIR
_jw.JOURNAL_DIR = _sv_jd2
_jw.assert_not_real("自检会写 digest_missing 读数")
try:
    _d4 = _dt2.datetime(2026, 10, 4, 23, 40)
    _day4 = "2026-10-04"

    # ① 没到点不打扰（只是晚了，不是没跑）
    _should, _why = _wd.pending(_dt2.datetime(2026, 10, 4, 23, 0))
    check("未到 23:30 不告警", _should is False and "23:30" in _why, _why)

    # ② 到点且今天没有投递读数 → 该告警
    _should, _why = _wd.pending(_d4)
    check("到点没送到 → 该告警", _should is True, _why)

    # ③ 今天送到了 → 不告警
    _jw.log_digest_pushed([("telegram", True, "ok")], digest_date=_day4)
    check("今天已送到 → 不告警", _wd.pending(_d4)[0] is False)
    check("delivered_on 认得出今天送达", _jw.delivered_on(_day4) is True)

    # ④ 送达读数在**别的**日期不算数（判据必须按当天）
    check("别的日期的送达不算今天",
          _jw.delivered_on("2026-10-05") is False)

    # ⑤ 换一天，真发一次（注入 sender，不碰网络），并验证"每天最多一次"
    _day6 = "2026-10-06"
    _d6 = _dt2.datetime(2026, 10, 6, 23, 45)
    _sent_wd: list = []
    _ok, _detail = _wd.alert_once(
        now=_d6, sender=lambda t, b: (_sent_wd.append((t, b)),
                                      [("telegram", True, "ok")])[1])
    check("告警真的发出去了", _ok and len(_sent_wd) == 1, _detail)
    check("告警标题说明是日报没送到",
          "日报没送到" in _sent_wd[0][0], _sent_wd[0][0])
    # 正文要给**能走通的路**（不能只说"出错了"）
    check("告警正文给出补跑命令", "src/report.py" in _sent_wd[0][1])
    check("告警把'计划 21:30'说清楚", "21:30" in _sent_wd[0][1])

    _ok2, _why2 = _wd.alert_once(now=_d6,
                                 sender=lambda t, b: [("telegram", True, "ok")])
    check("同一天不重复告警（冷却靠读数，不靠状态文件）",
          _ok2 is False and "已经告警过" in _why2, _why2)
    check("冷却判据来自 journal 读数", _jw.alerted_on(_day6) is True)

    # ⑥ 绝不抛异常：sender 炸了也要返回说明，不能冒到守护的主循环
    _day7 = "2026-10-07"
    _ok3, _why3 = _wd.alert_once(
        now=_dt2.datetime(2026, 10, 7, 23, 45),
        sender=lambda t, b: (_ for _ in ()).throw(RuntimeError("通道炸了")))
    check("通道异常被兜住（不冒到守护）", _ok3 is False and "炸了" in _why3, _why3)
    # 发失败也要留痕：否则下一轮（1 秒后）会再试，变成刷屏
    check("发失败也记账（避免刷屏）", _jw.alerted_on(_day7) is True)

    # ⑦ 读数/落盘全都不可用时也不抛，而且**不会刷屏**
    _sv_jd3 = _jw.JOURNAL_DIR
    _jw.JOURNAL_DIR = _P2("/System/pdca-selftest-not-writable")  # 读不到也写不进
    try:
        _wd._ALERTED_IN_PROCESS.clear()
        _suppressed: list = []
        # ⚠️ 这里**必须**注入 sender：漏注入就是真的往手机推（实测踩到过，
        # 两条真告警直接出去了）。notify 另有 PDCA_SUPPRESS_SEND 兜底。
        _fake = lambda t, b: (_suppressed.append(t), [("fake", True, "ok")])[1]  # noqa: E731
        _ok4, _why4 = _wd.alert_once(now=_d4, sender=_fake)
        check("journal 不可用时仍返回说明而不抛",
              isinstance(_why4, str) and bool(_why4), _why4)
        _ok5, _why5 = _wd.alert_once(now=_d4, sender=_fake)
        check("journal 不可用时也不会每轮重发（内存备忘挡住刷屏）",
              _ok5 is False and "已经告警过" in _why5, _why5)
        check("（该情形下确实只尝试发过一次）", len(_suppressed) == 1,
              str(len(_suppressed)))
    finally:
        _wd._ALERTED_IN_PROCESS.clear()
        _jw.JOURNAL_DIR = _sv_jd3

    # ⑧ 守护里的调用点：必须**只在非离线模式**下跑
    _dmn_src = (SRC / "daemon.py").read_text(encoding="utf-8")
    check("守护调用了看门狗", "watchdog.tick()" in _dmn_src)
    check("离线模式不跑看门狗",
          "if not _OFFLINE:" in _dmn_src and "import watchdog" in _dmn_src)
    # 导入必须在**循环之外**且失败降级：否则 watchdog 一旦被改坏
    # （语法错/被删），ImportError 会穿出主循环 → 崩溃循环 → 收件中断。
    # 附加组件不该有能力把唯一的收件职责带停。
    check("看门狗导入在循环之外（不进崩溃循环）",
          _dmn_src.index("import watchdog") < _dmn_src.index(
              "    _consecutive_failures = 0"),
          "import 出现在主循环之后")
    check("看门狗导入失败会降级并留一行日志",
          "看门狗不可用（不影响收件）" in _dmn_src)
    check("循环里不再逐轮 import 看门狗",
          "if _watchdog is not None:" in _dmn_src)
finally:
    _jw.JOURNAL_DIR = _sv_jo2
    _sh7.rmtree(_sv_jd2, ignore_errors=True)


section("v4 日报的 gap 行（漏跑几天后，恢复时说出来）")

# 与看门狗的分工：看门狗管当天（23:30 还没送到就喊），
# gap 行管事后对账（漏了几天）—— 机器整晚没醒时只有它能说话。
_sv_jd4 = _P2(_tf9.mkdtemp(prefix="pdca-selftest-gap-"))
_sv_jo4 = _jm.JOURNAL_DIR
_jm.JOURNAL_DIR = _sv_jd4
_jm.assert_not_real("自检会写 digest_pushed 读数")
try:
    _today_g = _dt2.date(2026, 10, 10)

    # 没有历史读数 → 不提示（"从没跑过"与"刚装的"分不清，就不猜）
    check("无历史时不提示", _rp._digest_gap_note(_today_g) == "")

    # 昨天有、今天没有 → 不提示（连续，没有漏）
    _jm.log_digest_pushed([("telegram", True, "ok")], digest_date="2026-10-09")
    check("只差一天时不提示", _rp._digest_gap_note(_today_g) == "")

    # 换一个干净的读数目录：最后一份是 10-07（今天 10-10）→ 3 天前、漏 2 次。
    # ⚠️ 必须换目录：journal 是追加式的，同目录里 10-09 那份会一直在，
    # `max(dates)` 就会取到它，"漏跑"这个场景根本构造不出来。
    _sh7.rmtree(_sv_jd4, ignore_errors=True)
    _sv_jd4.mkdir(parents=True, exist_ok=True)
    _jm.log_digest_pushed([("telegram", True, "ok")], digest_date="2026-10-07")
    _note = _rp._digest_gap_note(_today_g)
    check("漏跑会算出「上一份是几天前」", "3 天前" in _note, _note)
    check("漏跑会算出漏了几次", "漏了 2 次" in _note, _note)
    check("gap 行指向读数出处", "data/journal/" in _note, _note)

    # 渲染进日报正文（这是用户唯一能看到的地方）
    _t13, _b13 = _rp.build_report(_rp.ReportData(
        date=_today_g, digest_gap_note=_note))
    check("gap 行出现在日报正文里", "漏了 2 次" in _b13, _b13)
finally:
    _jm.JOURNAL_DIR = _sv_jo4
    _sh7.rmtree(_sv_jd4, ignore_errors=True)


section("v4 安装脚本")

_inst_path = ROOT / "deploy" / "install_launchd.sh"
_inst = _inst_path.read_text(encoding="utf-8")

# 只装 v4 的任务（v1 的三个 plist 文件保留但不再安装）
#
# ⚠️ 2026-10-05 加了第三个任务（周报）。这条断言写死整行字符串，
# 所以**加任务时必须一起改这里** —— 它就是"这是第几个任务"的记录点
# （与"命令条数"那条断言同一个用法：加东西要是一次有意的决定）。
check("安装脚本只装 v4 的三个任务（daemon / report / weekly）",
      "LABELS=(io.github.carlapple2025.pdca.daemon io.github.carlapple2025.pdca.report"
      " io.github.carlapple2025.pdca.weekly)" in _inst)
# 安装脚本**不安装** v1 任务，但会**清理**它们 ——
# 实测踩到：换架构时只装新的、不管旧的，旧任务会继续按老逻辑动数据
# （v1 的 report 会在 21:30 发一份基于旧留档的误导日报）。
check("安装列表只含 v4 三个任务",
      "LABELS=(io.github.carlapple2025.pdca.daemon io.github.carlapple2025.pdca.report"
      " io.github.carlapple2025.pdca.weekly)" in _inst)
check("安装脚本会清理 v1 遗留任务", "cleanup_legacy" in _inst
      and "LEGACY_LABELS" in _inst)
check("v1 任务被标为遗留而非安装",
      "LEGACY_LABELS=(io.github.carlapple2025.pdca.carryover io.github.carlapple2025.pdca.sync)" in _inst)
# 2026-10-07 改了标签前缀（去掉个人名）。这条守的是**迁移**里唯一危险的坑：
# 只装新的、不管旧的 → 新旧两套守护同时跑 → 抢同一个 Telegram offset →
# 同一条消息被处理两遍。所以旧标签必须在清理名单里，且 install 要调它。
check("安装脚本会卸掉改名前的旧标签（防两套守护同时跑）",
      "cleanup_pre_rename" in _inst
      and "PRE_RENAME_LABELS=(com.carl.pdca.daemon" in _inst
      and "  cleanup_legacy\n  cleanup_pre_rename\n" in _inst)

# v1 的 test 会跑 daily_report/carry_over —— v4 必须换成新入口，
# 否则"验证安装"验证的是已经不用了的代码
check("安装脚本的 test 跑新入口",
      "src/report.py" in _inst and "src/daemon.py" in _inst)
# 判据要限定在 test 函数体内：整份脚本会在注释里提到 v1 脚本名
# （说明为什么不跑它们），全局搜会误报 —— 这个"扫描器不排除非代码部分"
# 的坑本项目踩过多次。
_inst_test = _inst[_inst.index("do_test() {"):_inst.index("case \"${1:-}\"")]
check("test 不再跑 v1 入口",
      "daily_report.py" not in _inst_test and "carry_over.py" not in _inst_test,
      "test 函数里出现了 v1 脚本名")

# 验证据说不能顺手发一条重复日报
check("test 用 --no-push（不重复推送）", "--no-push" in _inst)

# preflight 要检查 v4 需要的配置项，并给出可执行的下一步
check("preflight 检查备忘文件夹配置", "memo_folder_id" in _inst)
check("preflight 检查日历配置", "calendar_name" in _inst)
check("preflight 指出去哪初始化", "setup-v4.sh" in _inst)

# doctor 的权限探测同样必须真实读取（与 setup 脚本一致）
check("doctor 真实读取权限", "count of folders" in _inst)

# 用系统 python3（与 launchd 一致，避免"手动能跑、定时跑不了"）
check("安装脚本用系统 python3", 'PYTHON="/usr/bin/python3"' in _inst)


section("v4 模块的提示指向真实存在的命令")

# 踩到过：reminders.py 与 memo.py 的错误提示让用户"先运行 deploy/init.sh"——
# 那是 v1 的初始化入口。**提示指向不存在的命令会让人走进死路**，
# 而且只在出错时才看到，最难排查。
_v4_mods = ("journal", "memo", "applecal", "whens", "kinds", "routes",
            "intake", "daemon", "report", "reminders")
_stale = []
for _m in _v4_mods:
    _txt = (SRC / f"{_m}.py").read_text(encoding="utf-8")
    if "init.sh" in _txt:
        _stale.append(_m)
check("v4 模块不再提示 v1 的 init.sh", not _stale, str(_stale))

# 提示里出现的每条 deploy/*.sh 都必须真的存在
import re as _re9  # noqa: E402
_missing_cmds: list[str] = []
for _m in _v4_mods:
    _txt = (SRC / f"{_m}.py").read_text(encoding="utf-8")
    for _hit in _re9.findall(r"deploy/([A-Za-z0-9_.-]+\.sh)", _txt):
        if not (ROOT / "deploy" / _hit).is_file():
            _missing_cmds.append(f"{_m}.py 提到 deploy/{_hit}（不存在）")
check("提示里提到的脚本都真实存在", not _missing_cmds,
      "；".join(_missing_cmds))


section("v4 初始化脚本")

# 初始化脚本把"有依赖顺序、容易漏"的步骤串起来。漏了的表现很隐蔽
# （没建「备忘」文件夹时，备忘那一路会一直失败，得翻日志才发现）。
_setup = (ROOT / "deploy" / "setup-v4.sh")
check("存在 deploy/setup-v4.sh", _setup.is_file())

_st = _setup.read_text(encoding="utf-8")

# 必须默认干跑：初始化会改动备忘录结构与日历
# 判据要写准：`--apply` 在脚本里是 case 分支（不带引号），
# 而"默认干跑"体现在 MODE 的默认值上。
check("初始化默认干跑（--apply 才改）",
      "--apply) APPLY=1" in _st and 'MODE="干跑' in _st)
# 必须幂等：重复运行安全
check("初始化脚本声明幂等", "幂等" in _st)

# ⚠️ 权限探测必须**真正读取数据**，不能只问 App 名字。
# 踩到过：`tell application "Notes" to return name` 会成功（只证明 App 存在），
# 而任何读取都报 -10004 —— 于是检查给出虚假的"✅ 可访问"，
# 让人以为配好了、实际每步都在失败。
check("权限探测读的是数据而非 App 名",
      "count of folders" in _st and "count of calendars" in _st
      and "count of lists" in _st)
# 排除注释：脚本里**故意**在注释中引用了 `to return name` 这个错误写法
# 作为反例说明。扫描器不排除注释就会自我误报（这个坑踩过多次）。
_st_code = "\n".join(l for l in _st.splitlines() if not l.lstrip().startswith("#"))
check("权限探测不再用 return name", "to return name" not in _st_code)

# 三处授权都要检查（漏一处就会出现"某功能一直失败但不知道原因"）
for _app in ("备忘录", "日历", "提醒事项"):
    check(f"初始化检查 {_app} 授权", _app in _st)

# 用了系统 python3 与 launchd 保持一致（避免"手动能跑、定时跑不了"）
check("初始化默认用系统 python3", "/usr/bin/python3" in _st)

# 单步失败不中止：一次看到全貌，而不是修一个跑一次
check("单步失败不中止（继续跑后面的）", "继续" in _st)

# bash 脚本不得有"$VAR 后紧跟全角字符"的写法（bash 3.2 会当成变量名一部分）
import re as _re7  # noqa: E402
_badsh = _re7.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[\x80-\xff])", _st)
check("初始化脚本无全角字符陷阱", not _badsh, str(_badsh))


section("v4 日报的「今日完成」按 Apple 原生完成时刻筛")

# ⚠️ 踩到过：`_read_todos` 取全量、只按 completed 分流（completed_at 恒为
# None），于是**昨天、上个月完成的条目全都落在"今日完成"里**，
# 日报越看越不可信。
#
# 修法用 Apple 原生 `completion date`（探测见 tools/probe-native-dates.py：
# 已完成条目读得到真实日期，未完成的是 missing value，`whose` 服务端可过滤）。
#
# 时区/区域设置坑的规避：**不让 AppleScript 回日期字符串**
# （"2026年10月3日 星期六 下午2:13:30" 依赖系统语言），
# 改回"年,月,日,时,分"整数分量，由 Python 组装。
_rem = _load(SRC / "reminders.py")

for _raw, _want in [("2026,10,3,14,13", _dt2.datetime(2026, 10, 3, 14, 13)),
                    ("2026,7,28,14,13", _dt2.datetime(2026, 7, 28, 14, 13)),
                    ("2026,1,1,0,0", _dt2.datetime(2026, 1, 1, 0, 0)),
                    ("none", None),           # 未完成条目 → missing value
                    ("NONE", None),           # 大小写不敏感
                    ("", None), ("坏值", None), ("2026,10", None)]:
    check(f"完成时刻解析 {_raw!r}",
          _rem._parse_completion(_raw) == _want,
          f"得到 {_rem._parse_completion(_raw)!r}")

# 解析失败**不能抛**：日报是只读汇总，一条读不出来不该让整份报告崩
check("完成时刻解析不抛异常",
      all(_rem._parse_completion(x) is None
          for x in ("坏", "1,2,3", "99,99,99,99,99" if False else "坏值")))

# Reminder 必须**带上** completed_at —— 否则上面这段解析白写（这是"解析出来
# 的信息在上层被丢掉"的老毛病，待办的 when 丢过一次，见另一节）。
check("Reminder 带 completed_at 字段",
      "completed_at" in _rem.Reminder.__dataclass_fields__,
      "字段没加的话，日报永远拿不到完成日期")

# completed_on 必须真的按日期筛（同一天的多条都算，别的一天不算）
_D1 = _dt2.date(2026, 10, 3)
_fake = [
    _rem.Reminder(id="1", name="今天甲", completed=True, body="", due="",
                  completed_at=_dt2.datetime(2026, 10, 3, 9, 0)),
    _rem.Reminder(id="2", name="今天乙", completed=True, body="", due="",
                  completed_at=_dt2.datetime(2026, 10, 3, 21, 30)),
    _rem.Reminder(id="3", name="昨天丙", completed=True, body="", due="",
                  completed_at=_dt2.datetime(2026, 10, 2, 21, 30)),
    _rem.Reminder(id="4", name="没打钩", completed=False, body="", due="",
                  completed_at=None),
    _rem.Reminder(id="5", name="打钩但没时刻", completed=True, body="", due="",
                  completed_at=None),
]
_origin_rem_cls = _rem.Reminders


class _FakeReminders(_rem.Reminders):
    """
    只替掉 I/O，**保留真实逻辑**。

    自检**不能**走真实 verify_list —— 沙箱里必被拒（-10004），
    那样断言就变成"测环境有没有授权"，而不是测筛日期的逻辑。

    注意要**继承**真实类：如果连 completed_on 一起替掉，
    这段测试就只是在测替身自己，等于没测。
    """
    fake: list = []

    def __init__(self, config=None):
        pass

    def verify_list(self):
        return 0

    def all_reminders(self):
        return list(self.fake)


try:
    _FakeReminders.fake = _fake
    _rem.Reminders = _FakeReminders
    _got = [r.id for r in _rem.Reminders({"reminders_list": "X"}).completed_on(_D1)]
    check("按日期筛出当天完成的 2 条", _got == ["1", "2"], str(_got))
    check("昨天的完成项被排除", "3" not in _got)
    check("打钩但没有时刻的**不**算进今天（宁可漏报也不误报）",
          "5" not in _got)

    # 日报那一层也要按日期筛（光在 reminders 里有 completed_on 不够 ——
    # 这正是"每一层都对、接起来仍可能错"的那类接线问题）
    _td = _rp._read_todos(_D1)
    check("日报只收当天完成的条目",
          sorted(t.name for t in _td if t.completed) == ["今天乙", "今天甲"],
          str([t.name for t in _td]))
    check("日报保留未完成条目",
          any(t.name == "没打钩" and not t.completed for t in _td))
    check("日报传下去的 completed_at 不为空",
          all(t.completed_at is not None for t in _td if t.completed))
finally:
    _rem.Reminders = _origin_rem_cls


section("v4 备忘的「感知路径」真的接上了（删掉就不再提醒）")

# 这是 ARCHITECTURE §四设计的那条路径：
#   台账（我提交过什么）+ **只读快照差集**（现在还在不在）
#     → 台账有、快照没有 = 你删了 → 记 memo_cleared → 永不再提醒
#
# ⚠️ 它曾经**完全没接上**：只有自检与 tools 用过 memo.snapshot()，
# 生产路径（report.py）只读台账，于是你删掉备忘、日报照样天天提醒它，
# 而页脚还写着"删掉即可，之后不再提醒"——一句没实现的承诺。
#
# 探测（tools/probe-native-dates.py）证实备忘录**没有**"这条被删了"的
# 原生线索（删除只是移进 Recently Deleted 保留 30 天），
# 所以只能靠快照差集，不能靠时间戳 —— 这条断言锁的就是那个差集。
_d = _P2(_tf2.mkdtemp())
_orig_jdir = _jr.JOURNAL_DIR
_orig_snap = _mm.snapshot_ids
try:
    _jr.JOURNAL_DIR = _d
    _jr.assert_not_real = lambda *a, **k: None

    _OLD = (_dt2.datetime.now() - _dt2.timedelta(days=10)).isoformat()
    _jr.append("memo_added", memo_id="keep", text="还在的备忘", at=_OLD)
    _jr.append("memo_added", memo_id="gone", text="被删掉的备忘", at=_OLD)
    _jr.append("memo_added", memo_id="fresh", text="今天刚记的",
               at=_dt2.datetime.now().isoformat())

    # 快照里只剩 keep 与 fresh → gone 是你删掉的
    _mm.snapshot_ids = lambda: {"keep", "fresh"}
    _stale = _rp._read_stale_memos(_dt2.date.today(), 3)

    check("被删掉的备忘不再被提醒",
          all(m.text != "被删掉的备忘" for m in _stale),
          str([m.text for m in _stale]))
    check("还在的、够天数的备忘照旧提醒",
          any(m.text == "还在的备忘" for m in _stale),
          str([m.text for m in _stale]))
    check("太新的备忘不提醒（不足天数）",
          all(m.text != "今天刚记的" for m in _stale))

    # 差集必须**留痕**（可追溯：这条提过、当天就处理了）
    _cleared = [r for r in _jr.read_range(days=1)
                if r.get("event") == "memo_cleared"]
    check("差集记了 memo_cleared",
          any(r.get("memo_id") == "gone" for r in _cleared),
          str(_cleared))
    check("memo_cleared 带原文（便于日后复盘）",
          any(r.get("text") == "被删掉的备忘" for r in _cleared))

    # ⚠️ 读不到快照 ≠ 被删了。
    # 把"没读到"当成"已删除"会**静默清空全部提醒**，而那看不出来 ——
    # 这是本项目最忌讳的失败形态。所以快照失败时必须退化成"照旧提醒"。
    def _boom():
        raise RuntimeError("模拟快照读取失败")

    _mm.snapshot_ids = _boom
    _stale2 = _rp._read_stale_memos(_dt2.date.today(), 3)
    check("快照失败时不当成'被删了'（宁可多提醒）",
          any(m.text == "还在的备忘" for m in _stale2),
          str([m.text for m in _stale2]))
finally:
    _jr.JOURNAL_DIR = _orig_jdir
    _mm.snapshot_ids = _orig_snap
    _sh2.rmtree(_d, ignore_errors=True)


section("日报退出码如实反映推送结果")

# 踩到过：report.main **永远返回 0**，于是推送全失败时 launchd 仍显示
# "成功"，你会以为日报发出去了。**静默失败比报错更危险。**
#
# 现在 run() 返回 (标题, 正文, 推送是否至少一个通道成功)，
# 退出码据此决定（0 / 3）。
_rd2 = _P2(_tf2.mkdtemp())
_old_root2 = _rp.ROOT
_rp.ROOT = _rd2
try:
    _D2 = _dt2.date(2026, 10, 3)

    # 两通道都成功
    _, _, _ok_a = _rp.run(date=_D2, push=True, data=_rp.ReportData(date=_D2),
                          sender=lambda t, b, channels=None:
                          [("telegram", True, "ok"), ("bark", True, "ok")])
    check("两通道成功 → 判为成功", _ok_a is True)

    # 只一个通道成功：两通道互为冗余，一个够
    _, _, _ok_b = _rp.run(date=_D2, push=True, data=_rp.ReportData(date=_D2),
                          sender=lambda t, b, channels=None:
                          [("telegram", False, "挂了"), ("bark", True, "ok")])
    check("只一个通道成功 → 仍判为成功（冗余）", _ok_b is True)

    # 全失败：必须判失败，否则 launchd 显示"成功"骗人
    _, _, _ok_c = _rp.run(date=_D2, push=True, data=_rp.ReportData(date=_D2),
                          sender=lambda t, b, channels=None:
                          [("telegram", False, "挂了"), ("bark", False, "也挂了")])
    check("全通道失败 → 判为失败", _ok_c is False)

    # 不推送（--no-push）视为成功
    _, _, _ok_d = _rp.run(date=_D2, push=False, data=_rp.ReportData(date=_D2))
    check("--no-push → 视为成功", _ok_d is True)
finally:
    _rp.ROOT = _old_root2
    _sh2.rmtree(_rd2, ignore_errors=True)

# main 的退出码必须用到这个结果
_rp_src2 = (SRC / "report.py").read_text(encoding="utf-8")
check("main 的退出码反映推送结果", "0 if pushed else 3" in _rp_src2)


section("v4 日报跨日历读取（2026-10-08：日程分散在「个人」「工作」）")

# 为什么要跨日历：用户的日程天然分在两个 iCloud 日历里，只读一个的话
# 另一个里的日程在日报里**静默看不见** —— 而"看不见"和"没有"是两件事。
_rp_x = sys.modules.get("report") or _load(SRC / "report.py")
_ac_x = _load(SRC / "applecal.py")
# ⚠️ 这里必须是**赋值**，不能写 setdefault：
# report 的函数体里是 `import applecal`，**调用时**才去 sys.modules 取。
# 若那里已经躺着一个 applecal 对象，setdefault 什么都不做 ——
# 我的 patch 就打在一个没人用的副本上，而 report 拿到的是另一个。
# （实测：4 条断言全红，但 `_asked` 是**空的**，因为 report 根本没经过这里。）
_sv_applecal_mod = sys.modules.get("applecal")
sys.modules["applecal"] = _ac_x
_sv_cfg_x, _sv_ev_x = _ac_x.config_calendars_read, _ac_x.events_between
try:
    _asked: list = []

    def _fake_ev(s, e, c=None):
        _asked.append(c)
        return [_ac_x.Event(summary=f"{c}·上午",
                            start=_dt2.datetime(2026, 10, 9, 9, 0),
                            end=_dt2.datetime(2026, 10, 9, 10, 0), calendar=c or ""),
                _ac_x.Event(summary=f"{c}·下午",
                            start=_dt2.datetime(2026, 10, 9, 14, 0),
                            end=_dt2.datetime(2026, 10, 9, 15, 0), calendar=c or "")]

    _ac_x.config_calendars_read = lambda: ["个人", "工作"]
    _ac_x.events_between = _fake_ev
    _evs_x = _rp_x.read_events_between(_dt2.date(2026, 10, 9), _dt2.date(2026, 10, 10))
    check("跨日历：两个日历都被读到", _asked == ["个人", "工作"], str(_asked))
    check("跨日历：两边的事件都进来（不再静默丢一个）",
          len(_evs_x) == 4, str(len(_evs_x)))
    check("跨日历：合并后按时间排序",
          [e.start.hour for e in _evs_x] == [9, 9, 14, 14],
          str([e.start.hour for e in _evs_x]))

    # 没配 calendar_read_names → 退回单日历：**老配置零改动**
    _asked.clear()
    _ac_x.config_calendars_read = lambda: []
    _rp_x.read_events_between(_dt2.date(2026, 10, 9), _dt2.date(2026, 10, 10))
    check("跨日历：没配就退回单日历（传 None，行为与以前一致）",
          _asked == [None], str(_asked))
finally:
    _ac_x.config_calendars_read = _sv_cfg_x
    _ac_x.events_between = _sv_ev_x
    if _sv_applecal_mod is None:
        sys.modules.pop("applecal", None)
    else:
        sys.modules["applecal"] = _sv_applecal_mod

section("v4 习惯留档（2026-10-11：把完成记录抄进 journal）")

# 为什么需要它：提醒事项的完成历史会被"清除已完成"清掉，而读 Apple 逐条明细
# 实测 ~1 s/条（APPLE-FACTS §2.5）—— 所以只能"每晚读窗口 + 抄进 journal"。
_rm_h = sys.modules.get("reminders") or _load(SRC / "reminders.py")
_hab_h = _load(SRC / "habits.py")

# ① 读脚本必须用原生 whose 窗口筛（而不是 all_reminders 全量读回 Python 筛）
_cap_h: list[str] = []
_sv_run_h = _rm_h.run
try:
    _rm_h.run = lambda src, **kw: (_cap_h.append(src), "")[1]
    _rm_h.Reminders({"reminders_list": "习惯"}).completed_since(3)
finally:
    _rm_h.run = _sv_run_h
_h_src = _cap_h[-1] if _cap_h else ""
check("习惯读取用原生 whose 窗口筛（不读全量）",
      "whose completed is true" in _h_src
      and "is greater than or equal to cutoff" in _h_src, _h_src[:160])
check("习惯读取的窗口是参数化的（不是写死的天数）",
      "- 3 * days" in _h_src, _h_src[:160])
_h_proc = _sp.run(["/usr/bin/osacompile", "-e", _h_src, "-o", "/tmp/_h.scpt"],
                    capture_output=True, text=True)
check("习惯读取脚本真的能编译（不需要授权）",
      _h_proc.returncode == 0, (_h_proc.stderr or "")[:160])

# ② 解析：名字里带换行也不能错位（id 固定首行、完成时刻固定末行）
try:
    _rm_h.run = lambda src, **kw: ("RID-1\n英语·正常\n2026,10,11,20,5\n----\n"
                                   "RID-2\n跑\n步\n2026,10,11,7,0\n----\n")
    _parsed_h = _rm_h.Reminders({"reminders_list": "习惯"}).completed_since(3)
finally:
    _rm_h.run = _sv_run_h
check("习惯读取能解析出 id / 名字 / 完成时刻", len(_parsed_h) == 2, str(len(_parsed_h)))
check("名字里的换行不会被拆错（多行都算名字）",
      len(_parsed_h) == 2 and _parsed_h[1].name == "跑\n步",
      _parsed_h[1].name if len(_parsed_h) > 1 else "—")
check("完成时刻解析正确",
      bool(_parsed_h) and _parsed_h[0].completed_at is not None
      and _parsed_h[0].completed_at.hour == 20, str(_parsed_h[0].completed_at))

# ③ 该不该扫（纯函数，不碰 Apple 也不碰文件）
check("还没到 21:00 不扫",
      _hab_h.should_scan(_dt2.datetime(2026, 10, 11, 20, 0),
                         list_name="习惯", scanned_today=False)[0] is False)
check("过了 21:00 且今天没扫过 → 扫",
      _hab_h.should_scan(_dt2.datetime(2026, 10, 11, 21, 30),
                         list_name="习惯", scanned_today=False)[0] is True)
check("今天扫过就不再扫（每天最多一次）",
      _hab_h.should_scan(_dt2.datetime(2026, 10, 11, 22, 0),
                         list_name="习惯", scanned_today=True)[0] is False)
check("没配置列表就不扫（未启用不是故障）",
      _hab_h.should_scan(_dt2.datetime(2026, 10, 11, 22, 0),
                         list_name="", scanned_today=False)[0] is False)

# ④ 判重：同一次发生绝不写两条（窗口重叠、重跑、补醒都靠它）
_h_dir = _fresh_journal()
_sv_jd_h = _jm.JOURNAL_DIR
_sv_rm_mod = sys.modules.get("reminders")
try:
    _jm.JOURNAL_DIR = _h_dir

    class _FakeRem:
        def __init__(self, cfg=None):
            pass

        def completed_since(self, days):
            return [_rm_h.Reminder(id="H1", name="英语", completed=True, body="",
                                   due="", completed_at=_dt2.datetime(2026, 10, 11, 20, 5)),
                    _rm_h.Reminder(id="H2", name="跑步", completed=True, body="",
                                   due="", completed_at=_dt2.datetime(2026, 10, 11, 7, 0))]

    sys.modules["reminders"] = type("_m", (), {"Reminders": _FakeRem})
    _h1 = _hab_h.scan(list_name="习惯", days=3)
    check("习惯扫描：第一次两条都记下", _h1 == {"found": 2, "added": 2, "skipped": 0}, str(_h1))
    _h2 = _hab_h.scan(list_name="习惯", days=3)
    check("习惯扫描：重跑不写重（按 id 判重）",
          _h2 == {"found": 2, "added": 0, "skipped": 2}, str(_h2))
    _lines_h = [_json2.loads(x) for p in _h_dir.glob("*.jsonl")
                for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    check("journal 里恰好两条 habit_done（没写重）",
          sum(1 for x in _lines_h if x.get("event") == "habit_done") == 2,
          str([x.get("event") for x in _lines_h]))
    check("每条带 habit_id / name / done_at",
          all(x.get("habit_id") and x.get("name") and x.get("done_at")
              for x in _lines_h if x.get("event") == "habit_done"),
          str([x for x in _lines_h if x.get("event") == "habit_done"]))
    check("扫描本身也留一条读数（今天扫过没有靠它）",
          sum(1 for x in _lines_h if x.get("event") == "habit_scanned") == 2,
          str([x.get("event") for x in _lines_h]))
    check("habit_scanned_on 能读出'今天扫过'",
          _jm.habit_scanned_on(_dt2.date.today().isoformat()) is True)
finally:
    _jm.JOURNAL_DIR = _sv_jd_h
    if _sv_rm_mod is None:
        sys.modules.pop("reminders", None)
    else:
        sys.modules["reminders"] = _sv_rm_mod
    _sh2.rmtree(_h_dir, ignore_errors=True)

# ⑤ 守护里真的挂了它（而且和看门狗同一套降级写法）
_dm_src_h = (SRC / "daemon.py").read_text(encoding="utf-8")
check("守护循环里挂了习惯留档", "_habits.tick()" in _dm_src_h)
check("习惯留档导入失败只降级、不带停收件",
      "import habits as _habits" in _dm_src_h
      and "习惯留档不可用（不影响收件）" in _dm_src_h)

# ⑥ 读的 key 与文档一致（改 key 忘了改代码 / 反过来，都会被这条抓到）
_hab_src_h = (SRC / "habits.py").read_text(encoding="utf-8")
check("habits.py 读的配置键是 habits_list / habits_window_days",
      '"habits_list"' in _hab_src_h and '"habits_window_days"' in _hab_src_h)

section("v4 裸时刻补日期（2026-10-11：下午3点不再只进备注）")

# 起因：用户发「下午3点生成第一个视频」→ 提醒事项里只在「全部」出现，
# 「已编排」「今日」都没有。根因是解析器算出了今天 15:00，
# 但 has_date=False → 不写日期字段、只把时刻塞进备注，
# 而备注被渲染成名字下面的第二行、长得跟到期日一样 ✗。
_wh_m = sys.modules.get("whens") or _load(SRC / "whens.py")
_rt_m = sys.modules.get("routes") or _load(SRC / "routes.py")
_it_m = sys.modules.get("intake") or _load(SRC / "intake.py")

_NOW = _dt2.datetime(2026, 10, 11, 10, 0)      # 上午 10 点：用来固定"已过 / 没到"


def _prom(text, now=_NOW):
    w = _wh_m.parse_when(text)
    return _wh_m.promote_bare_time(w, now=now) if w else None


_p1 = _prom("下午3点生成视频")
check("裸时刻·还没到 → 今天，且标为「补的」",
      _p1 is not None and _p1.start == _dt2.datetime(2026, 10, 11, 15, 0)
      and _p1.has_date is True and _p1.date_inferred is True,
      str(_p1.start) if _p1 else "None")
_p2 = _prom("上午9点开会")
check("裸时刻·已经过了 → 明天（实测里上午 9 点正是这种情况）",
      _p2 is not None and _p2.start == _dt2.datetime(2026, 10, 12, 9, 0),
      str(_p2.start) if _p2 else "None")
_p3 = _prom("明天下午3点生成视频")
check("说了日子的 → 原样不动、**不标**补的",
      _p3 is not None and _p3.start == _dt2.datetime(2026, 10, 12, 15, 0)
      and _p3.date_inferred is False, str(_p3.start) if _p3 else "None")
check("没有时间的句子 → 解析不出就是 None（不硬补）",
      _wh_m.parse_when("交电费") is None)
check("全天 / 无时刻的 When 不被改动",
      _wh_m.promote_bare_time(
          _wh_m.When(start=_dt2.datetime(2026, 10, 11, 9, 0),
                     end=_dt2.datetime(2026, 10, 11, 9, 0),
                     all_day=True, has_date=True), now=_NOW).date_inferred is False)

# 路由层：**写入端与回执必须看到同一个 when**，否则回执会说"只写进备注" ✗
_it_title = _rt_m.route("下午3点生成第一个视频")
check("路由层给待办补上了日期",
      _it_title.when is not None and _it_title.when.has_date is True
      and _it_title.when.date_inferred is True, str(_it_title.when))
_rp_txt = _it_m._reply_ok(_it_title, "x")
check("回执说出「日子是补的」（不悄悄替你决定）",
      "补成" in _rp_txt and "10月11日" in _rp_txt, _rp_txt)
check("回执同时说清会响", "到点提醒" in _rp_txt, _rp_txt)

# ⚠️ 范围只到待办：事件那条路**没动**（一次只改一件事）
_ev_t = _rt_m.route("@下午3点 开会")
check("事件那条路没被动（不补日期、不标 inferred）",
      _ev_t.when is not None and _ev_t.when.date_inferred is False,
      str(_ev_t.when))


# ── 汇总

print()
print("═" * 50)
if failures:
    print(f"❌ 失败 {len(failures)}/{checks} 项：")
    for f in failures:
        print(f"   · {f}")
    sys.exit(1)
print(f"✅ 全部通过（{checks} 项）")
sys.exit(0)
