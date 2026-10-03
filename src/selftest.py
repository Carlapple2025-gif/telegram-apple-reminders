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
check("旧 @时段 前缀被剥掉（兼容历史笔记）", r[4][1] == "时段丁")

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

_WHOSE_OK = _re5.compile(r"whose\s+\w+\s+(is|contains|is not)\b")
_hits5: list[str] = []
_scanned5 = 0
for _f in list((ROOT / "src").glob("*.py")) + list((ROOT / "deploy").glob("*.sh")):
    if _f.name == "selftest.py":
        continue
    for _ln, _line in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
        _stripped = _line.lstrip()
        # 跳过注释：注释里会引用反例（"whose id \"...\""），误报过
        if _stripped.startswith("#"):
            continue
        for _m in _re5.finditer(r"whose\s+\w+", _line):
            _scanned5 += 1
            _tail = _line[_m.start():_m.start() + 60]
            if not _WHOSE_OK.search(_tail):
                _hits5.append(f"{_f.name}:{_ln}  {_tail.strip()[:50]}")

check(f"{_scanned5} 个 whose 子句都带 is/contains",
      not _hits5 and _scanned5 > 5,
      "；".join(_hits5[:3]) if _hits5 else f"只扫描到 {_scanned5} 个，可能扫描失败")

# 不能出现重复的 is（批量替换时误伤过，产生 "is is"）
_dup5: list[str] = []
for _f in list((ROOT / "src").glob("*.py")) + list((ROOT / "deploy").glob("*.sh")):
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

# 日报默认走双通道，且支持 --ask（渐进式披露的落地点）
_dr_src = (SRC / "daily_report.py").read_text(encoding="utf-8")
check("日报默认双通道", "telegram,bark" in _dr_src)

# 定时任务里必须真的带上 --ask，否则"接了但没启用"
_plist = (ROOT / "deploy" / "com.carl.pdca.report.plist").read_text(encoding="utf-8")
check("定时任务的日报已启用 --sync", "<string>--sync</string>" in _plist)


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

section("关键路线的定时任务")

# 关键路线（最小模型）：
#   ① 09:00 同步待办 → 提醒事项   ← 没它就没地方打钩，完成状态无从谈起
#   ② 21:30 日报（只读 + 推送）
#   ③ 07:00 顺延（唯一写备忘录）
#
# 检查 plist 真的存在且在安装列表里 —— "实现了但没接线"是本项目
# 反复踩的坑（write_back 未被调用、顺延后不建留档、待办从未同步）。
_plists = {p.name for p in (ROOT / "deploy").glob("com.carl.pdca.*.plist")}
for _need in ("com.carl.pdca.sync.plist", "com.carl.pdca.report.plist",
              "com.carl.pdca.carryover.plist"):
    check(f"存在 {_need}", _need in _plists)

_install = (ROOT / "deploy" / "install_launchd.sh").read_text(encoding="utf-8")
for _lbl in ("com.carl.pdca.sync", "com.carl.pdca.report", "com.carl.pdca.carryover"):
    check(f"安装列表含 {_lbl}", _lbl in _install)

# 同步任务必须带 --apply，否则只干跑、待办永远进不了提醒事项
_sync_plist = (ROOT / "deploy" / "com.carl.pdca.sync.plist").read_text(encoding="utf-8")
check("同步任务带 --apply", "<string>--apply</string>" in _sync_plist)
check("同步任务带 --refresh", "<string>--refresh</string>" in _sync_plist)


# ── 顺延必须依据**当前**权威状态

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
_shell_files = sorted((ROOT / "deploy").glob("*.sh"))
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


section("v4 命令分类（classify）")

_cls = _load(SRC / "classify.py")

# 待办：有动作动词
for _s in ["交电费", "勘察表盖章", "明天交电费", "跟进修缮", "买猫粮",
           "回复邮件", "提交结算单", "盖章"]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} → 待办", _c is not None and _c.kind == _cls.Kind.TODO,
          f"得到 {_c.kind.value if _c else None}")

# 日程：有事件名词
for _s in ["周五下午两点项目周会", "下周三体检", "10月8日评审会", "周一上午开庭"]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} → 日程", _c is not None and _c.kind == _cls.Kind.EVENT,
          f"得到 {_c.kind.value if _c else None}")

# 备忘：有备忘信号词
for _s in ["想起一件事，荷载要按名称命名", "记一下这个想法"]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} → 备忘", _c is not None and _c.kind == _cls.Kind.MEMO,
          f"得到 {_c.kind.value if _c else None}")

# 周期 → 日程 + RRULE（日历独有的重复能力；提醒事项不支持 repeat）
for _s, _rrule in [("每周一交周报", "FREQ=WEEKLY;BYDAY=MO"),
                   ("每天跑步", "FREQ=DAILY"),
                   ("每个工作日站会", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
                   ("每月1日交房租", "FREQ=MONTHLY;BYMONTHDAY=1"),
                   ("每周五例会", "FREQ=WEEKLY;BYDAY=FR")]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} → 日程", _c is not None and _c.kind == _cls.Kind.EVENT,
          f"得到 {_c.kind.value if _c else None}")
    check(f"{_s!r} 的 RRULE", _c.recurrence == _rrule, f"得到 {_c.recurrence!r}")
    check(f"{_s!r} 有起始日", _c.when is not None)

# 周期事件的起始日必须落在正确的星期
_c = _cls.classify("每周一交周报", _B)
check("每周一 → 起始日落在周一", _c.when.start.date() == _dt2.date(2026, 10, 5),
      f"得到 {_c.when.start.date()}")
_c = _cls.classify("每周日大扫除", _B)
check("每周日 → 起始日落在周日", _c.when.start.date() == _dt2.date(2026, 10, 4),
      f"得到 {_c.when.start.date()}")

# 需确认（真的判不出）—— 这是"判断归用户"的落点
for _s in ["帮我看下那个表", "那个东西弄一下"]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} → 需确认", _c.confidence == _cls.Confidence.ASK)
    check(f"{_s!r} 给了三个候选", len(_c.candidates) == 3)

# 动作优先于"有时间" —— 明天交电费是待办，不是日程
for _s in ["明天交电费", "周五上午交材料", "下周一提交报告"]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} 是待办而非日程", _c.kind == _cls.Kind.TODO)

# 正文剥离（回执里显示的应该是"事情本身"）
for _s, _want in [("明天交电费", "交电费"), ("每天跑步", "跑步"),
                  ("每月1日交房租", "交房租"), ("周五下午两点项目周会", "项目周会"),
                  ("下周三体检", "体检")]:
    _c = _cls.classify(_s, _B)
    check(f"{_s!r} 的正文", _c.text == _want, f"得到 {_c.text!r}")

check("空输入 → None", _cls.classify("", _B) is None)
check("纯空白 → None", _cls.classify("   ", _B) is None)


section("v4 用户日志（journal）")

_jr = _load(SRC / "journal.py")
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

# memo 模块**不能有删除能力** —— 这是架构约束（用户保留删除权）
_mm_src = (SRC / "memo.py").read_text(encoding="utf-8")
check("memo 模块不含 delete 命令",
      "delete " not in _mm_src.replace("# ", ""))


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


def _extract_applescript(path, extra_ns=None):
    """把源码里的 AppleScript f-string 求值出来（占位符填 "X"）。"""
    _tree = _ast5.parse(path.read_text(encoding="utf-8"))
    _ns = {"_as_literal": lambda s: '"X"', "_html_escape": lambda s: "X",
           "FSEP": _mm.FSEP, "RSEP": _mm.RSEP}
    for _k in ("fid", "note_id", "cal", "uid", "name", "text", "summary",
               "first_line", "body_html", "location", "description",
               "recurrence", "props", "scope", "start", "end"):
        _ns[_k] = "X"
    _ns.update(extra_ns or {})

    _out = []
    for _node in _ast5.walk(_tree):
        if not isinstance(_node, _ast5.JoinedStr):
            continue
        try:
            _v = eval(compile(_ast5.Expression(_node), "<s>", "eval"), _ns)
        except Exception:
            continue          # 求值不了的跳过（不是 AppleScript 模板）
        if isinstance(_v, str) and _v.lstrip().startswith("tell application"):
            _out.append(_v)
    return _out


_as_bad: list[str] = []
_as_total = 0
for _mod in ("memo.py", "applecal.py"):
    for _src5 in _extract_applescript(SRC / _mod):
        _as_total += 1
        _r5 = _sp5.run(["osacompile", "-o", _os5.devnull, "-e", _src5],
                       capture_output=True, text=True)
        if _r5.returncode != 0:
            _as_bad.append(f"{_mod}: {_r5.stderr.strip()[:50]}")

check(f"v4 的 {_as_total} 段 AppleScript 全部可编译",
      not _as_bad and _as_total >= 4,
      "；".join(_as_bad) if _as_bad else f"只找到 {_as_total} 段，可能提取失败")

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
