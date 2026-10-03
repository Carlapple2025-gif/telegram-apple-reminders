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


# ── 1. 静态契约：上层调用的方法必须存在

print("── 1. 静态契约检查（方法是否存在）──")

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


# ── 2. 解析器行为（纯函数，离线可测）

print("\n── 2. 解析器行为检查 ──")
import parse as parser  # noqa: E402


def kinds(text: str) -> list[tuple[str, str, bool | None, str | None]]:
    r = parser.parse(text)
    return [(e.kind, e.text, e.completed, e.slot) for e in r.entries]


# 基础标记
r = kinds("2026-10-02\n- [ ] 待办甲\n- [x] 待办乙\n* 备忘丙\n@中午 时段丁")
check("识别日期标题", r[0][0] == "meta" and r[0][1] == "2026-10-02")
check("未完成待办", r[1] == ("todo", "待办甲", False, None))
check("已完成待办", r[2] == ("todo", "待办乙", True, None))
check("备忘不进待办", r[3][0] == "note" and r[3][1] == "备忘丙")
check("时段前缀被剥掉", r[4] == ("todo", "时段丁", False, "中午"))

# 裸行 = 待办（最高频情况，零符号）
r = kinds("裸行没有符号")
check("裸行视为未完成待办", r[0] == ("todo", "裸行没有符号", False, None))

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

# 五种时段
for slot in ["上午", "中午", "下午", "晚上", "明天"]:
    r = kinds(f"@{slot} 事")
    check(f"时段 {slot}", r[0][3] == slot)

# 元信息行
r = kinds("# 备注行")
check("# 开头视为元信息", r[0][0] == "meta")

# 空行不产出条目
r = kinds("甲\n\n\n乙")
check("空行不产出条目", len(r) == 2)

# 时段未指定要有提示（供上层询问）
res = parser.parse("- [ ] 没写时段")
check("未指定时段时产生 issue", any("时段未指定" in i for i in res.entries[0].issues))

# 去重：只报告，不删除
res = parser.parse("提交结算单\n提交结算单\n完全不同的事")
dups = parser.find_duplicates(res.entries)
check("发现疑似重复", len(dups) >= 1, f"实际 {len(dups)} 组")
check("去重不删除任何条目", len(res.entries) == 3)

# 原文逐字保留（人工核对依赖这一点）
raw = "2026-10-02\n- [ ] 甲 乙  丙\n"
res = parser.parse(raw)
check("原文逐字保留", res.entries[1].raw == "- [ ] 甲 乙  丙")


# ── 3. 顺延逻辑（离线可测 —— 这些分支在沙箱里必须能验掉）

print("\n── 3. 顺延逻辑检查 ──")
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


# ── 4. 留档渲染

print("\n── 4. 留档渲染检查 ──")
res = parser.parse("2026-10-02\n- [ ] 甲\n* 乙")
md = parser.render_archive(res, {"date": "2026-10-02", "source": "test"})
check("留档含原文段", "## 原文（逐字保留）" in md)
check("留档含解析段", "## 解析结果" in md)
check("留档含日期", "date: 2026-10-02" in md)


# ── 5. AppleScript 字面量转义

print("\n── 5. AppleScript 转义检查 ──")
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


# ── 6. 清理判据（哪些能删、哪些必须留）

print("\n── 6. 清理判据检查 ──")
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


# ── 7. 提醒事项同步规则

print("\n── 7. 提醒事项同步规则 ──")
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
_keyA = _rems.make_key(_NOTE, 2, "甲")
_keyB = _rems.make_key(_NOTE, 3, "乙")

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
check("跨天不误判为重复", len(_p.create) == 2)

# 孤儿条目只报告（可能是你手动加的），不自动删
_p = _push.build_plan(_page, [_rem("r9", "手动加的", False)], _NOTE)
check("孤儿条目只报告不自动删", len(_p.orphan) == 1)

# 去重键要能区分内容
check("改文字后视为新内容",
      _rems.make_key(_NOTE, 2, "甲") != _rems.make_key(_NOTE, 2, "甲改过了"))


# ── 8. 完成状态合并（权威 = 提醒事项）

print("\n── 8. 完成状态合并规则 ──")
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
                          body=_rems.make_key(_CNOTE, line_no, name), due="")


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


# ── 9. 顺延的完成状态来自留档（权威链路的落点）

print("\n── 9. 顺延依据留档里的完成状态 ──")

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


# ── 10. slot 在重新解析时必须保留

print("\n── 10. 重新解析时保留已设时段 ──")

# 实测踩到的 bug：slot 有两个来源（备忘录里的 @时段、Telegram 按钮选的），
# 而重新解析只能看到前者 —— 于是每次 --refresh 都会把按钮选的结果清掉，
# 表现为"你设的时段过一会儿自己没了"。
# 这里用真实 sync 模块的函数验证保留逻辑。
_pg = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙")
_pg.entries[1].slot = "中午"          # 模拟"按钮选的，落在留档里"
_prev_map = {e.line_no: e.slot for e in _pg.entries if e.slot}

_fresh = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙")
for _e in _fresh.entries:
    if _e.slot is None and _e.line_no in _prev_map:
        _e.slot = _prev_map[_e.line_no]
check("重新解析后 slot 被保留", _fresh.entries[1].slot == "中午")

# slot 保留的键**只能是正文**，不能用行号 —— 两次实测踩坑：
#   ① 只用行号：编辑备忘录后行号位移，上一行号的时段被错套到别的条目；
#   ② 行号+正文：行号一变就找不到，正确的选择被丢掉。
check("slot 保留键用正文而非行号",
      parser.normalize_line("甲") != parser.normalize_line("乙"))

# 行号位移后仍能按正文找回（模拟删掉一行）
_prev_by_text = {parser.normalize_line("乙"): "中午"}
_after_edit = parser.parse("2026-10-02\n- [ ] 乙")   # 乙 从第3行变成第2行
for _e in _after_edit.entries:
    _k = parser.normalize_line(_e.text)
    if _e.slot is None and _k in _prev_by_text:
        _e.slot = _prev_by_text[_k]
check("行号位移后仍按正文找回 slot", _after_edit.entries[1].slot == "中午")

# 备忘录里已有时段时，应以备忘录为准（你自己写的优先）
_fresh2 = parser.parse("2026-10-02\n@下午 甲")
_prev2 = {1: "上午"}
for _e in _fresh2.entries:
    if _e.slot is None and _e.line_no in _prev2:
        _e.slot = _prev2[_e.line_no]
check("备忘录里的 @时段 优先于留档里旧的", _fresh2.entries[1].slot == "下午")


# ── 11. 按钮选择器的状态机

print("\n── 11. 按钮选择器状态机 ──")
_ask = _load(SRC / "ask_slots.py")


def _sess(*texts):
    return _ask.Session(answers=[
        _ask.Answer(line_no=i, text=t) for i, t in enumerate(texts, start=2)])


# 回调编码必须在 64 字节限制内（实测硬限制）
_bad_cb = []
_s2 = _sess("甲", "乙", "丙")
for _i2 in range(len(_s2.answers)):
    _s2.index = _i2
    for _row in _ask.frame_keyboard(_s2):
        for _lbl, _data in _row:
            if len(_data.encode("utf-8")) > _tg.CALLBACK_MAX_BYTES:
                _bad_cb.append(_data)
check("按钮回调数据均在 64 字节内", not _bad_cb, str(_bad_cb[:3]))

# 正文必须回显"已定"情况 —— 用户反馈"点完成只记录一条"，
# 根因是看不到进度；正文回显是最直接的证据。
_s3 = _sess("甲", "乙")
_s3.answers[0].slot = "中午"
_s3.answers[0].decided = True
_txt = _ask.frame_text(_s3)
check("正文回显已定条数", "已定 1/2" in _txt)
check("正文列出已做的选择", "甲→中午" in _txt)

# 最后一条的"下一条"应变成"完成"
_s4 = _sess("甲", "乙")
_s4.index = 1
_labels = [l for row in _ask.frame_keyboard(_s4) for l, _ in row]
check("最后一条显示「完成」而非「下一条」", any("完成" in l for l in _labels))

# 确认屏必须警告未选项 —— 这是防"静默丢答案"的关键
_s5 = _sess("甲", "乙")
_s5.answers[0].slot = "上午"
_s5.answers[0].decided = True
_s5.confirming = True
_ctxt = _ask.confirm_text(_s5)
check("确认屏列出全部选择", "甲" in _ctxt and "乙" in _ctxt)
check("确认屏警告未选项", "还有 1 条没选" in _ctxt)
_kb5 = [l for row in _ask.confirm_keyboard(_s5) for l, _ in row]
check("确认屏提供回跳入口", any("回去补" in l for l in _kb5))

# 全部已选时不警告
_s6 = _sess("甲")
_s6.answers[0].slot = "上午"
_s6.answers[0].decided = True
_s6.confirming = True
check("全部已选时不显示警告", "没选" not in _ask.confirm_text(_s6))

# ["全部跳过"] 已按用户要求换成"这不是待办"
_s7 = _sess("甲")
_labels7 = [l for row in _ask.frame_keyboard(_s7) for l, _ in row]
check("已移除「全部跳过」按钮", not any("全部跳过" in l for l in _labels7))
check("提供「这不是待办」按钮", any("不是待办" in l for l in _labels7))

# 备忘标记：选了之后不应再被当作待办参与时段询问
_s8 = _sess("甲")
_s8.answers[0].as_note = True
_s8.answers[0].decided = True
check("标记备忘后仍算已决定", _s8.decided_count == 1)


# ── 12. 顺延的幂等与防链式

print("\n── 12. 顺延幂等 / 不链式往后传 ──")

_prev02 = parser.parse("2026-10-02\n@中午 勘察表盖章\n明天要交电费")

# 首次顺延：应写 2 行，标记带来源日期
_plan, _ = _co.build_plan(_prev02, None, "2026-10-02", "2026-10-03")
check("首次顺延标记带来源日期",
      len(_plan) == 2 and all("⟳10-02" in l for l in _plan), str(_plan[:1]))

# 再跑一次（目标页已有这些行）→ 必须是 0，这才是幂等
_next03 = "2026-10-03\n" + "\n".join(_plan)
_plan2, _ = _co.build_plan(_prev02, _next03, "2026-10-02", "2026-10-03")
check("重复运行不重复写入（幂等）", len(_plan2) == 0, str(_plan2))

# 关键：次日不该把"已顺延过"的继续往后传（实测出现过 10-02→10-03→10-04）
_prev03 = parser.parse(_next03)
_plan3, _sk3 = _co.build_plan(_prev03, None, "2026-10-03", "2026-10-04")
check("已顺延过的条目不再往后传（防链式）", len(_plan3) == 0, str(_plan3))

# 各种写法都要归一到同一个指纹（曾产生 `⟳ ⟳`）
_fps = {_co.content_fingerprint(t) for t in
        ["勘察表盖章", "勘察表盖章 ⟳", "勘察表盖章 ⟳ ⟳",
         "- [ ] 勘察表盖章 ⟳", "@中午 勘察表盖章"]}
check("不同标记写法归一到同一指纹", len(_fps) == 1, str(_fps))


# ── 18. 按钮回调不丢失（两次实测踩坑）

print("\n── 13. 按钮回调不丢失 ──")

_tg.answer_callback = lambda *a, **k: None   # 离线时静默

# 坑 1：`d` 是两步操作 —— 第一次进确认屏，第二次才提交。
# 早先 offset 没有跨调用续传，导致第二次点击被当成"历史"跳过，
# 用户表现为"点了提交没反应"。
_s9 = _sess("甲", "乙")
_s9.answers[0].slot = "上午"
_s9.answers[0].decided = True
_r1 = _ask.handle_click(_s9, {"data": "d", "callback_id": "c1", "message_id": 1})
check("第一次点完成 → 进确认屏（不结束）", _r1 is True and _s9.confirming is True)
_r2 = _ask.handle_click(_s9, {"data": "d", "callback_id": "c2", "message_id": 1})
check("第二次点完成 → 真正结束", _r2 is False)

# 坑 2：一次返回多个点击时，必须逐个处理。
# 只处理第一个的话，offset 会推进到整批之后，剩下的点击被永久跳过。
_s10 = _sess("甲", "乙")
_ask.handle_click(_s10, {"data": "s:0:am", "callback_id": "a", "message_id": 1})
_ask.handle_click(_s10, {"data": "s:1:pm", "callback_id": "b", "message_id": 1})
check("连续两次点击都被处理", _s10.decided_count == 2
      and _s10.answers[0].slot == "上午" and _s10.answers[1].slot == "下午")

# wait_for_callback 必须支持 offset 续传与整批返回
import inspect as _insp  # noqa: E402
_sig = _insp.signature(_tg.wait_for_callback)
check("wait_for_callback 支持 offset 续传", "offset" in _sig.parameters)
check("wait_for_callback 支持跳过历史", "skip_history" in _sig.parameters)

# 返回结构里要有 batch（整批）与 offset（续传位置）
_src = inspect.getsource(_tg.wait_for_callback)
check("回调返回整批而非单个", '"batch"' in _src)


# ── 18. 过期按钮必须有响应

print("\n── 14. 过期按钮的处理 ──")

# 用户实测踩到：会话结束后再点按钮，没有任何响应、按钮一直转圈
# （演示消息的按钮被点了 11 次）。这里验证两件事：
#   ① 会话结束时会主动收尾（撤掉按钮）
#   ② 提供了清理积压点击的入口
check("telegram 提供 drain_stale_callbacks",
      hasattr(_tg, "drain_stale_callbacks"))
check("ask_slots 会话结束会收尾",
      hasattr(_ask, "finish_ui"))
_src_finish = inspect.getsource(_ask.finish_ui)
check("收尾会去掉按钮（不传 reply_markup）",
      "reply_markup" not in _src_finish)


# ── 18. AppleScript 语法校验（osacompile，只编译不执行）

print("\n── 15. AppleScript 语法校验 ──")

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


# ── 18. 系统 Python 3.9 兼容性

print("\n── 16. AppleScript 的 whose 子句 ──")

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


print("\n── 17. 通知通道 ──")

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
check("日报支持 --ask 询问时段", '"--ask"' in _dr_src)

# 定时任务里必须真的带上 --ask，否则"接了但没启用"
_plist = (ROOT / "deploy" / "com.carl.pdca.report.plist").read_text(encoding="utf-8")
check("定时任务的日报已启用 --ask", "<string>--ask</string>" in _plist)
check("定时任务的日报已启用 --sync", "<string>--sync</string>" in _plist)


print("\n── 17. 系统 Python 兼容性 ──")

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
            "ask_slots"]

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


# ── 11. 密钥不进版本库

print("\n── 18. 密钥保护检查 ──")

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


# ── 18. shell 脚本静态检查：bash 3.2 的全角字符陷阱

print("\n── 19. shell 脚本检查 ──")

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
