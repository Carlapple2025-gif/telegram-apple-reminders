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
    return carry_over.build_plan(prev, next_text)


# 基本顺延
plan, _ = plan_of("2026-10-02\n- [ ] 甲\n- [x] 乙\n- [ ] 丙", None)
check("只顺延未完成项", len(plan) == 2 and any("甲" in l for l in plan) and any("丙" in l for l in plan))
check("已完成项被排除", not any("乙" in l for l in plan))

# 备忘不参与
plan, _ = plan_of("2026-10-02\n- [ ] 甲\n* 备忘丙", None)
check("备忘不被顺延", len(plan) == 1)

# 顺延行的格式
plan, _ = plan_of("2026-10-02\n- [ ] 甲", None)
check("顺延行带 ⟳ 标记", plan and plan[0].endswith(carry_over.CARRY_MARK))
check("顺延行是未完成复选框", plan and plan[0].startswith("- [ ] "))

# 幂等性 —— 这是实测踩到的 bug：次日页里是「甲 ⟳」，来源是「甲」，
# 不剥标记就比不相等，于是同一条被顺延第二遍。
plan, skipped = plan_of("2026-10-02\n- [ ] 甲", "2026-10-03\n- [ ] 甲 ⟳\n")
check("幂等：已有「甲 ⟳」时不重复顺延", len(plan) == 0, f"实际 {plan}")

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
_plan, _ = _co.build_plan(_prev_done, None)
check("留档标记为完成的条目不参与顺延",
      len(_plan) == 1 and "甲" in _plan[0], f"实际 {_plan}")

# 全部完成 → 无事可做
_prev_all = parser.parse("2026-10-02\n- [ ] 甲\n- [ ] 乙")
for _e in _prev_all.entries:
    _e.completed = True
_plan, _ = _co.build_plan(_prev_all, None)
check("留档里全部完成 → 不顺延任何条目", len(_plan) == 0)


# ── 10. AppleScript 语法校验（osacompile，只编译不执行）

print("\n── 9. AppleScript 语法校验 ──")

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


# ── 11. 系统 Python 3.9 兼容性

print("\n── 11. 系统 Python 兼容性 ──")

# 为什么单独查这个：launchd 任务用的是 **/usr/bin/python3（3.9）**，
# 而我平时用自带运行时（3.12）。若代码用了运行时求值的类型标注
# （如 dataclass 字段上的 `str | None`），本地测得好好的，
# 定时任务里却 import 失败 —— 那是最难查的一类问题。
# 所以这里**真的用系统 python3 导入一遍**，而不是只做语法解析
# （ast.parse 只验语法、不求值注解，抓不到这类问题）。
import subprocess as _sp2  # noqa: E402

_SYS_PY = "/usr/bin/python3"
_modules = ["parse", "notes", "sync", "notify", "reminders", "push_tasks",
            "cleanup", "daily_report", "read_day", "completion", "telegram"]

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

print("\n── 12. 密钥保护检查 ──")

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


# ── 13. shell 脚本静态检查：bash 3.2 的全角字符陷阱

print("\n── 13. shell 脚本检查 ──")

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
