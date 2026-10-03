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

# 属性名可以是两个词（`whose completion date is greater than d`），
# 而且源码里这个子句常常跨行 —— 跨行处会插进 `'` 换行 缩进 `'`
# （Python 相邻字符串字面量拼接），所以间隙要允许这些字符，不能只允许 \w\s。
_WHOSE_OK = _re5.compile(r"whose.{0,40}?(is|contains)\b", _re5.S)
_hits5: list[str] = []
_scanned5 = 0
for _f in list((ROOT / "src").glob("*.py")) + list((ROOT / "deploy").glob("*.sh")):
    if _f.name == "selftest.py":
        continue
    _lines5 = _f.read_text(encoding="utf-8").splitlines()
    # 逐行跟踪"是否在文档字符串里"：docstring 里提到 whose 是在讲事情，
    # 不是在写 AppleScript。（本项目扫描器的老毛病：把 docstring 里的
    # 函数名当成调用、把注释里的反例当成代码 —— 这次提前处理掉。）
    _in_doc = False
    for _ln, _line in enumerate(_lines5, 1):
        _stripped = _line.lstrip()
        _quotes = _line.count('"""')
        if _quotes == 1:
            _in_doc = not _in_doc
            continue                      # 定界行本身不算
        if _in_doc:
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
for _sh in sorted((ROOT / "deploy").glob("*.sh")):
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
    "com.carl.pdca.*.plist"))
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

# v4 的关键路线只有两个任务：
#   ① 常驻收件守护（KeepAlive）—— 你发一句就有人接
#   ② 21:30 日报（只读三处快照）
#
# v1 的三个任务（09:00 同步 / 21:30 日报 / 07:00 顺延）在 v4 都不需要：
# 待办常驻提醒事项，没有"同步"和"顺延"这两个概念。
_plists = {p.name for p in (ROOT / "deploy").glob("com.carl.pdca.*.plist")}
for _need in ("com.carl.pdca.daemon.plist", "com.carl.pdca.report.plist"):
    check(f"存在 {_need}", _need in _plists)

_install = (ROOT / "deploy" / "install_launchd.sh").read_text(encoding="utf-8")
for _lbl in ("com.carl.pdca.daemon", "com.carl.pdca.report"):
    check(f"安装列表含 {_lbl}", _lbl in _install)

# 守护必须 KeepAlive（否则退出后没人接消息）
_dmn = (ROOT / "deploy" / "com.carl.pdca.daemon.plist").read_text(encoding="utf-8")
check("守护任务设了 KeepAlive", "<key>KeepAlive</key>" in _dmn)
check("守护任务有重启节流", "ThrottleInterval" in _dmn)
check("守护指向 daemon.py", "src/daemon.py" in _dmn)

# 日报指向新实现，且不带 v1 的 --refresh/--sync（v4 日报是纯只读）
_rep = (ROOT / "deploy" / "com.carl.pdca.report.plist").read_text(encoding="utf-8")
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


section("分类判据收紧（真实误判的教训）")

# 实测踩到：用户发了一段 89 字的感慨，被判成**待办**写进了提醒事项。
# 根因：`交`、`写`、`学` 这类单字动词**命中任意位置**就算数。
# 现在收紧为：动词要在开头附近 + 正文不太长。
_VERBOSE = ("这个年代真正要学的是原理和工程思想，例如浏览器如何工作，"
            "React 背后的实现，JS的单线程本质，如何测试等等，知识面至少是"
            "个全栈，至于编码那种茴字四种写法的事情，交给 AI 吧")
_c9 = _cls.classify(_VERBOSE, _B)
check("长感慨不再判成待办（真实误判）",
      _c9.kind != _cls.Kind.TODO,
      f"得到 {_c9.kind.value}")

# 真实待办必须仍然判对（收紧不能误伤）
for _s in ("交电费", "跟进修缮", "记得给车做保养", "整理上季度所有客户的合同并按地区分类归档"):
    _c = _cls.classify(_s, _B)
    check(f"{_s[:12]!r} 仍判待办", _c.kind == _cls.Kind.TODO,
          f"得到 {_c.kind.value}")

# 判据本身：动词要在开头附近
check("动词在开头 → 祈使句", _cls._verb_leads("交电费"))
check("动词在句中 → 非祈使句", not _cls._verb_leads(_VERBOSE))
check("待办长度上限存在", hasattr(_cls, "TODO_MAX_LEN"))


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
    # 允许少 1 个：`applecal.add()` 的脚本是**运行时拼装**的
    # （依赖 props 的 join 结果），静态求值器拿不到，只能少这一块。
    # 那一块由下面单独的直接验证覆盖，不是漏检。
    if _block_n < _stmt_n - 1:
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
# 它的 AppleScript 是运行时拼装的（依赖 props 的 join 结果），
# 静态提取器拿不到，所以在这里**按同样方式拼一遍**再编译。
# 覆盖：日期逐字段 set 的写法、with properties {...} 的组装。
import datetime as _dt6  # noqa: E402

_ac = sys.modules.get("applecal") or _load(SRC / "applecal.py")
_add_src = (
    f'tell application "Calendar"\n'
    f'  set targetCal to calendar "X"\n'
    + _ac._set_date_script("startDate", _dt6.datetime(2026, 10, 5, 14, 0))
    + _ac._set_date_script("endDate", _dt6.datetime(2026, 10, 5, 15, 0))
    + '  set newEv to make new event at end of events of targetCal '
      'with properties {summary:"X", start date:startDate, end date:endDate, '
      'location:"X", recurrence:"FREQ=WEEKLY;BYDAY=MO"}\n'
    '  return uid of newEv\n'
    'end tell')
_r6 = _sp5.run(["osacompile", "-o", _os5.devnull, "-e", _add_src],
               capture_output=True, text=True)
check("applecal.add 的脚本形状可编译", _r6.returncode == 0,
      _r6.stderr.strip()[:80])

# 逐字段设日期是刻意选择（避开受区域设置影响的 date 字面量），
# 确认它真的在生成脚本里
check("日期用逐字段 set（不用 date 字面量）",
      "set year of startDate to 2026" in _add_src
      and 'date "' not in _add_src)

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

    def todo(self, text, when=None):
        self.calls.append(("todo", text, when))
        return "T-1"

    def event(self, summary, start, end, location="", recurrence="", allday=False):
        self.calls.append(("event", summary, start, end, recurrence, allday))
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
    check("待办：ok 且类型正确", _o.ok and _o.kind == _cls.Kind.TODO)
    check("待办：只写了一次", len(_f.calls) == 1, f"得到 {_f.calls}")
    check("待办：写的是提醒事项端", _f.calls[0][0] == "todo")
    check("待办：正文剥掉时间词", _f.calls[0][1] == "交电费", _f.calls[0][1])
    check("待办：回执说明去向", "提醒事项" in _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ② 日程 → 只写日历（含时间与重复规则）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("周五下午两点项目周会", _B)
    check("日程：类型正确", _o.kind == _cls.Kind.EVENT)
    check("日程：写的是日历端", _f.calls[0][0] == "event")
    check("日程：时间算对", _f.calls[0][2] == _dt2.datetime(2026, 10, 9, 14, 0),
          str(_f.calls[0][2]))
    check("日程：回执含日期", "10月9日" in _o.reply)

    _i, _f = _new_intake()
    _o = _i.handle("每周一交周报", _B)
    check("周期日程：带 RRULE", _f.calls[0][4] == "FREQ=WEEKLY;BYDAY=MO",
          _f.calls[0][4])
    check("周期日程：按全天处理", _f.calls[0][5] is True)
    check("周期日程：回执说人话", "每周一" in _o.reply, _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ③ 备忘 → 只写备忘录
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("想起一件事，荷载要按名称命名", _B)
    check("备忘：类型正确", _o.kind == _cls.Kind.MEMO)
    check("备忘：写的是备忘录端", _f.calls[0][0] == "memo")
    check("备忘：回执说明去向", "备忘录" in _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ④ 判不出 → **不写入**、要求确认（"判断归用户"的落点）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("帮我看下那个表", _B)
    check("判不出：不写入任何端", len(_f.calls) == 0, f"得到 {_f.calls}")
    check("判不出：要求确认", _o.needs_ask and not _o.ok)
    check("判不出：给了三个候选", len(_o.candidates) == 3)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# ⑤ 日程没写时间 → 不瞎猜一个时间，要求补充
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("例会", _B)
    check("日程缺时间：不写入", len(_f.calls) == 0)
    check("日程缺时间：要求补充", _o.needs_ask)
    check("日程缺时间：回执提示", "没写时间" in _o.reply)
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
    _i.handle("周五下午两点项目周会", _B)
    _i.handle("想起一件事，备忘内容", _B)
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

# ⑩ 判不出时也要记 input —— 便于复盘"我提过但没记成"
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("帮我看下那个表", _B)
    _evs = [r["event"] for r in _jr.read_day(_jr._today())]
    check("判不出也记 input", "input" in _evs, str(_evs))
    check("判不出不写入", len(_f.calls) == 0)
finally:
    _sh2.rmtree(_d, ignore_errors=True)


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


section("v4 补时间要接到上一条上（不新建）")

# 这是上面那次踩坑的**修复断言**。用户被追问"日程没写时间"后回一句
# 「上午九点」——它必须接到上一条上，而不是拿"上午九点"当标题新建。
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle("上午九点", _B, msg_id=90,
                   pending_text="测试Apple- agent稳定性")
    check("补时间：合并后只写一条", len(_f.calls) == 1, f"得到 {_f.calls}")
    check("补时间：写的是日历端", _f.calls[0][0] == "event")
    check("补时间：标题是上一条的原文（不是「上午九点」）",
          _f.calls[0][1] == "测试Apple- agent稳定性", repr(_f.calls[0][1]))
    check("补时间：回执如实说明'接在上一条'", "接在你上一条上" in _o.reply)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# 没有上一条时**不能瞎接** —— 单独一句"上午九点"仍按独立日程处理。
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("上午九点", _B, msg_id=91, pending_text=None)
    check("无上一条时不合并（标题仍是原句）",
          _f.calls[0][1] == "上午九点", repr(_f.calls[0][1]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# 一句完整的话不该被 pending 影响 —— pending 是个"补时间"的口子，
# 不能变成"任何话都往上一条上接"。
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("明天上午九点测试 Apple agent 稳定性", _B, msg_id=92,
              pending_text="别的旧条目")
    check("完整的一句话不受 pending 影响",
          _f.calls[0][1] == "测试 Apple agent 稳定性", repr(_f.calls[0][1]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)


section("v4 只说时刻、而该时刻今天已过 → 顺延明天")

# whens.py 是纯函数，刻意不读时钟（否则没法离线测），并在注释里写明
# "由调用方决定（它本来就知道'现在'）"。调用方是 intake —— 这一段验它
# 真的做了这件事。不做的话会出现"下午两点开会"在晚上说、
# 日程却落在**今天下午两点（已经过去）**。
#
# 用"今天已过的时刻"构造：取当前时刻往前 2 小时，再取整点。
_now = _dt2.datetime.now()
_past = (_now - _dt2.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
_past_s = f"{_past.hour}点"

_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _o = _i.handle(f"{_past_s} 项目周会", _B)
    _got = _f.calls[0][2]
    check("已过的时刻被顺延到次日",
          _got.date() == _past.date() + _dt2.timedelta(days=1),
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
    _i.handle("明天下午两点 项目周会", _B)
    check("写了日期就不顺延",
          _f.calls[0][2] == _dt2.datetime(2026, 10, 4, 14, 0),
          str(_f.calls[0][2]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)


section("v4 待办的时间信息不能被丢掉")
# 踩到过：classify 在待办分支写 `when=None`，把已经解析好的时间扔了 ——
# "明天交电费"里的"明天"白解析，上层再也拿不到。
# **解析出来的信息不该在分类这一步被丢弃**，用不用是上层的事。
_c_todo = _cls.classify("明天交电费", _B)
check("待办也保留解析出的时间", _c_todo.when is not None,
      "when 被丢掉了")
check("待办的 when 日期正确",
      _c_todo.when is not None and _c_todo.when.start.date() == _dt2.date(2026, 10, 4),
      str(_c_todo.when.start.date() if _c_todo.when else None))
check("待办的 when 标了 has_date",
      _c_todo.when is not None and _c_todo.when.has_date is True)

_c_todo2 = _cls.classify("交电费", _B)
check("无时间的待办 when 为 None", _c_todo2.when is None)

# intake 要把这个时间传给提醒事项端（是否设 due 由该端决定）
_d = _fresh_journal()
try:
    _i, _f = _new_intake()
    _i.handle("明天交电费", _B)
    check("intake 把时间传给了待办端",
          _f.calls[0][2] is not None
          and _f.calls[0][2].date() == _dt2.date(2026, 10, 4),
          str(_f.calls[0][2]))
finally:
    _sh2.rmtree(_d, ignore_errors=True)


section("v4 收件守护（daemon，状态持久化）")

_dm = _load(SRC / "daemon.py")

# offset 必须持久化：只存内存的话，进程重启会重新拉到旧消息，
# **同一条被记两遍**（重复建待办）。这与 v1 的按钮 bug 同源 ——
# 跨调用的读取位置必须持久。
_dm_dir = _P2(_tf2.mkdtemp())
_dm.STATE_FILE = _dm_dir / "state.json"
_dm.PENDING_DIR = _dm_dir / "pending"
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

    # 待补充项落盘 → 按钮不依赖内存（进程重启后照样能用）
    _dm.save_pending(42, "帮我看下那个表")
    check("待补充项可读回", _dm.load_pending() == "帮我看下那个表")

    # ⚠️ **只有一个槽位**：这条断言锁住"不需要猜接哪一条"这个设计。
    # 曾经按消息号各存一份，于是"点按钮 → 系统追问 → 用户补时间"
    # 会留下两份记录，补时间时只能猜 —— 端到端实测的结果是
    # `有多条待补充，无法确定接哪条 → 不合并`，修复等于没生效。
    _dm.save_pending(77, "后来的一条")
    check("新的一条会顶掉旧的（只有一个槽位）",
          _dm.load_pending() == "后来的一条", str(_dm.load_pending()))

    # ⚠️ 而且按钮**必须解析当前槽位，不能拿消息号去卡**。
    # 踩到过：追问是另一条新消息，槽位在那个消息号上，按钮却绑原消息号，
    # 于是点任何一个都只回"这条已经处理过了（或已过期）"，
    # 而追问文案还在说"点下面的按钮"—— 用户点下去就是撞墙。
    # 只有一个槽位，"当前那个"就是唯一答案。
    check("按钮不靠消息号卡（能解析当前槽位）",
          _dm.load_pending(12345) == "后来的一条",
          "用别的消息号也应解析到唯一的当前槽位")
    _dm.clear_pending(12345)
    check("清槽位也不靠消息号卡",
          _dm.load_pending() is None)

    # 过期的待补充不再认（隔一天的"补时间"接上去只会更困惑）
    _dm.PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _dm._pending_path().write_text(_json2.dumps(
        {"text": "过期的那条", "msg_id": 9,
         "at": (_dt2.datetime.now()
                - _dt2.timedelta(hours=_dm.PENDING_TTL_HOURS + 1)).isoformat()},
        ensure_ascii=False), encoding="utf-8")
    check("过期（超 TTL）的待补充不再认", _dm.load_pending() is None,
          "过期的补时间接上去只会更让人困惑")

    _dm.clear_pending()
    check("清除后为 None", _dm.load_pending() is None)
    _dm.clear_pending()
    check("重复清除不崩", True)

    # 升级兼容：只存在旧格式（<msg_id>.json）时也要认，并且**清对文件**。
    # 不认它的话，升级瞬间正好挂着一条待补充，用户补时间就接不上 ——
    # 又回到"标题变成时间"那个 bug。
    _legacy = _dm.PENDING_DIR / "84.json"
    _dm.PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _legacy.write_text(_json2.dumps(
        {"text": "旧格式的待补充", "at": _dt2.datetime.now().isoformat()},
        ensure_ascii=False), encoding="utf-8")
    check("能读旧格式的待补充项",
          _dm.load_pending() == "旧格式的待补充", str(_dm.load_pending()))
    _dm.clear_pending()
    check("清的是旧格式那个文件（没留垃圾）",
          not _legacy.exists() and _dm.load_pending() is None)
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

# 按钮指定类型时必须走同一条分派路径（跳过分类但不跳过校验）
_d = _fresh_journal()
try:
    _f2 = _FakeSinks()
    _it2 = _it.Intake(add_todo=_f2.todo, add_event=_f2.event, add_memo=_f2.memo)
    _o = _dm._dispatch_forced(_it2, _cls.Kind.TODO, "帮我看下那个表")
    check("按钮指定待办 → 写到提醒事项端",
          _f2.calls and _f2.calls[0][0] == "todo", str(_f2.calls))

    _f3 = _FakeSinks()
    _it3 = _it.Intake(add_todo=_f3.todo, add_event=_f3.event, add_memo=_f3.memo)
    _o = _dm._dispatch_forced(_it3, _cls.Kind.MEMO, "帮我看下那个表")
    check("按钮指定备忘 → 写到备忘录端",
          _f3.calls and _f3.calls[0][0] == "memo", str(_f3.calls))

    # 日程缺时间仍要求补充 —— 没有时间的日程在日历里没有意义
    _f4 = _FakeSinks()
    _it4 = _it.Intake(add_todo=_f4.todo, add_event=_f4.event, add_memo=_f4.memo)
    _o = _dm._dispatch_forced(_it4, _cls.Kind.EVENT, "开会")
    check("按钮指定日程但缺时间 → 不写入、要求补充",
          len(_f4.calls) == 0 and _o.needs_ask)
finally:
    _sh2.rmtree(_d, ignore_errors=True)

# daemon 依赖的 telegram 接口必须存在（改了 telegram 会在这里断掉）
for _fn in ("send", "send_with_buttons", "answer_callback", "get_updates",
            "load_config"):
    check(f"telegram 提供 {_fn}", hasattr(_tg, _fn))


section("v4 整链：发消息 → 按钮 → 补时间（用户实测的那条路）")

# ⚠️ 这个 bug **活着到了用户手上**，原因就是自检只测到 intake 那一层：
#   intake 层单测全绿（"给 pending_text 就能合并"），
#   但 daemon 层"点按钮后把待补充项清掉了"，于是补时间时没有上一条可接。
#   实测链条：发「测试Apple- agent稳定性」→ 判不准 → 点「日程」
#   → 追问"再说一次带上时间" → 回「上午九点」
#   → **标题变成「上午九点」、时间落在今天 09:00（已过去）、原标题丢失**。
#
# 教训：**跨模块的接线本身也要测**。每一层都对，接起来仍可能是错的。
_d = _fresh_journal()
_dm_dir2 = _P2(_tf2.mkdtemp())
_saved = (_dm.PENDING_DIR, _dm._make_intake, _dm.tg.send,
          _dm.tg.send_with_buttons, _dm.tg.answer_callback)
_dm.PENDING_DIR = _dm_dir2 / "pending"
try:
    _sent = []
    _mids = [1000]

    def _swb(_t, _kb):
        _mids[0] += 1
        _sent.append(_t)
        return {"message_id": _mids[0]}

    _wrote = []
    _sinks = _FakeSinks()

    def _make(_sinks=_sinks, _wrote=_wrote):
        return _it.Intake(
            add_todo=lambda t, w=None: (_wrote.append(("todo", t)), "T1")[1],
            add_event=lambda s, a, b, **k: (_wrote.append(("event", s, a)), "E1")[1],
            add_memo=lambda t: (_wrote.append(("memo", t)), "M1")[1],
            testing=True)

    _dm._make_intake = _make
    _dm.tg.send = lambda t: _sent.append(t)
    _dm.tg.send_with_buttons = _swb
    _dm.tg.answer_callback = lambda *a, **k: None

    # ① 用户发消息 → 判不准 → 存下待补充 + 发按钮
    _dm.handle_message("测试Apple- agent稳定性", 84, "chat")
    check("整链①：判不准时存下待补充项",
          _dm.load_pending() == "测试Apple- agent稳定性", str(_dm.load_pending()))
    check("整链①：发出了确认按钮", _dm.load_pending() is not None)

    # ② 点「日程」按钮 → 缺时间 → 追问，且**待补充项必须还在**
    _dm._handle_callback("e:84", "cb1", "chat")
    check("整链②：缺时间时不写入日历", not _wrote, str(_wrote))
    check("整链②：追问后待补充项仍在（这是曾经漏掉的一环）",
          _dm.load_pending() == "测试Apple- agent稳定性", str(_dm.load_pending()))

    # ③ 回一句纯时间 → 必须接到上一条上，标题是原文
    _dm.handle_message("上午九点", 89, "chat")
    check("整链③：写了一条且只写一条", len(_wrote) == 1, str(_wrote))
    check("整链③：写的是日历端", _wrote[0][0] == "event", str(_wrote))
    check("整链③：标题是原来的事由（不是「上午九点」）",
          _wrote[0][1] == "测试Apple- agent稳定性", repr(_wrote[0][1]))
    check("整链③：回执说明接在上一条", "接在你上一条上" in _sent[-1])
    check("整链③：用掉后槽位清空", _dm.load_pending() is None,
          str(_dm.load_pending()))
finally:
    (_dm.PENDING_DIR, _dm._make_intake, _dm.tg.send,
     _dm.tg.send_with_buttons, _dm.tg.answer_callback) = _saved
    _sh2.rmtree(_d, ignore_errors=True)
    _sh2.rmtree(_dm_dir2, ignore_errors=True)


section("v4 整链：追问之后按钮仍然点得动")

# ⚠️ 这一段是 subagent 审出来的 bug：追问那一步把槽位挪到了**追问消息号**，
# 按钮却仍绑**原消息号** —— 两组按钮的 callback_data 全对不上槽位，
# 点任何一个都只回"这条已经处理过了（或已过期）"，
# 而追问自己的文案还在说"（要改成待办/备忘，点下面的按钮）"。
#
# 只有"直接发一句时间"（文本路径）是通的 —— 所以上一条整链测试
# **测不到**它：那条路径没有点按钮。又一次说明"每一层都对，接起来仍可能错"。
_d = _fresh_journal()
_dm_dir3 = _P2(_tf2.mkdtemp())
_saved3 = (_dm.PENDING_DIR, _dm._make_intake, _dm.tg.send,
           _dm.tg.send_with_buttons, _dm.tg.answer_callback)
_dm.PENDING_DIR = _dm_dir3 / "pending"
try:
    _sent3 = []
    _mids3 = [2000]
    _kb_seen = []

    def _swb3(_t, _kb):
        _mids3[0] += 1
        _sent3.append(_t)
        _kb_seen.append(_kb)
        return {"message_id": _mids3[0]}

    _wrote3 = []
    _dm._make_intake = lambda: _it.Intake(
        add_todo=lambda t, w=None: (_wrote3.append(("todo", t)), "T1")[1],
        add_event=lambda s, a, b, **k: (_wrote3.append(("event", s, a)), "E1")[1],
        add_memo=lambda t: (_wrote3.append(("memo", t)), "M1")[1],
        testing=True)
    _dm.tg.send = lambda t: _sent3.append(t)
    _dm.tg.send_with_buttons = _swb3
    _dm.tg.answer_callback = lambda *a, **k: None

    # ① 判不准 → 按钮
    _dm.handle_message("测试Apple- agent稳定性", 84, "chat")
    # ② 点「日程」→ 缺时间 → 追问 + 槽位仍在
    _dm._handle_callback("e:84", "cb1", "chat")
    check("追问后：槽位仍在等补充", _dm.load_pending() == "测试Apple- agent稳定性")
    check("追问后：又发了一组按钮给用户", len(_kb_seen) >= 2,
          f"共发了 {len(_kb_seen)} 组按钮")

    # ③ **点追问那组按钮** —— callback_data 里的消息号与槽位里的并不相同
    _slot_mid = _dm._load_pending_record().get("msg_id")
    _btn_mid = int(_kb_seen[-1][0][0][1].split(":")[1])
    check("（前提）按钮消息号与槽位消息号确实不同 —— 正是当初撞墙的条件",
          _btn_mid != _slot_mid,
          f"按钮={_btn_mid} 槽位={_slot_mid}")
    _dm._handle_callback(f"t:{_btn_mid}", "cb2", "chat")
    check("追问后的按钮点得动（不再回'已经处理过了'）",
          "已经处理过了" not in _sent3[-1], _sent3[-1])
    check("追问后的按钮真的写入了",
          _wrote3 and _wrote3[-1][0] == "todo", str(_wrote3))
    check("追问后的按钮用的是原来那条的原文",
          _wrote3[-1][1] == "测试Apple- agent稳定性", repr(_wrote3[-1][1]))
    check("用掉后槽位清空", _dm.load_pending() is None)
finally:
    (_dm.PENDING_DIR, _dm._make_intake, _dm.tg.send,
     _dm.tg.send_with_buttons, _dm.tg.answer_callback) = _saved3
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
for _kw in ("summary", "start", "end", "location", "recurrence", "allday"):
    check(f"applecal.add 接受 {_kw}", _kw in _sig_ev.parameters)
# intake 调的是 add(summary, start, end, location=..., recurrence=..., allday=...)
_sig_ev.bind(None, _dt2.datetime(2026, 10, 5), _dt2.datetime(2026, 10, 5, 1),
             location="", recurrence="", allday=False)
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

# ③ 备忘端：memo.add(text) → Memo.note_id
_sig_memo = _insp8.signature(_mm8.add)
_sig_memo.bind("x")
check("memo.add(text) 是合法调用", True)
check("memo.Memo 有 note_id 字段（intake 要取它）",
      "note_id" in {f.name for f in _dc8.fields(_mm8.Memo)})

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
_daemon_plist = (ROOT / "deploy" / "com.carl.pdca.daemon.plist").read_text(
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
      and 'if [ "$label" = "com.carl.pdca.daemon" ]; then' in _inst3)
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
    _oout2 = _oit.handle("周五下午两点项目周会", _B)
    _ocalls = getattr(_oit, "_offline_calls", [])
    check("离线模式：日程指向日历",
          any("日历" in c for c in _ocalls), str(_ocalls))
    # 判不出的情况照样要问，且不写
    _n_before = len(_ocalls)
    _oout3 = _oit.handle("帮我看下那个表", _B)
    check("离线模式：判不出仍要求确认", _oout3.needs_ask)
    check("离线模式：判不出不写入",
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
    date=_RD, errors=["提醒事项：拒绝访问"]))
check("提醒事项读取失败时不说\"没有待办\"", "还没有条目" not in _b8b)
check("提醒事项读取失败时有告警", "⚠️" in _b8b)

# 读取失败要如实标注，不能静默变成"今天没有待办"
_t9, _b9 = _rp.build_report(_rp.ReportData(
    date=_RD, todos=[_rp.Todo("甲", True)],
    errors=["日历：超时", "备忘台账：文件损坏"]))
check("读取失败时标注告警", "⚠️" in _b9)
check("读取失败时列出来源", "日历：超时" in _b9 and "备忘台账：文件损坏" in _b9)
check("读取失败时说明可能不完整", "可能不完整" in _b9)

# 无地点不该显示多余的 @
_t10, _b10 = _rp.build_report(_rp.ReportData(
    date=_RD, events=[_rp.Event("例会", _dt2.datetime(2026, 10, 4, 9, 0))]))
check("日程无地点时不显示 @", "@" not in _b10)

# run() 要存档到 data/digest/ 且不推送（注入假 sender）
import tempfile as _tf7  # noqa: E402
import shutil as _sh7  # noqa: E402
_dg = _P2(_tf7.mkdtemp())
_old_root = _rp.ROOT
_rp.ROOT = _dg
try:
    _sent: list = []
    _rp.run(date=_RD, push=True, data=_rp.ReportData(date=_RD),
            sender=lambda t, b, channels=None: (_sent.append((t, b)), [("telegram", True, "ok")])[1])
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


section("v4 安装脚本")

_inst_path = ROOT / "deploy" / "install_launchd.sh"
_inst = _inst_path.read_text(encoding="utf-8")

# 只装 v4 的两个任务（v1 的三个 plist 文件保留但不再安装）
check("安装脚本只装 daemon 与 report",
      "LABELS=(com.carl.pdca.daemon com.carl.pdca.report)" in _inst)
# 安装脚本**不安装** v1 任务，但会**清理**它们 ——
# 实测踩到：换架构时只装新的、不管旧的，旧任务会继续按老逻辑动数据
# （v1 的 report 会在 21:30 发一份基于旧留档的误导日报）。
check("安装列表只含 v4 两个任务",
      "LABELS=(com.carl.pdca.daemon com.carl.pdca.report)" in _inst)
check("安装脚本会清理 v1 遗留任务", "cleanup_legacy" in _inst
      and "LEGACY_LABELS" in _inst)
check("v1 任务被标为遗留而非安装",
      "LEGACY_LABELS=(com.carl.pdca.carryover com.carl.pdca.sync)" in _inst)

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
_v4_mods = ("journal", "memo", "applecal", "classify", "whens",
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
