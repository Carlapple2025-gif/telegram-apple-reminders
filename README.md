# pdca — Telegram 进、Apple 应用出

**用一句话说**：你对 Telegram 发一句自然语言，Mac mini 把它分派到对应的 Apple 应用
（提醒事项 / 日历 / 备忘录），21:30 自动生成日报推回来。

**当前状态：v4，守护与日报都在跑。** 你在 Apple 应用里打钩 / 改字 / 删除 —— 那才是唯一状态源。

> ⚠️ 这份 README 写的是 **v4**。上一版描述的是 v1（备忘录当天页 + 顺延 + 三个定时任务），
> 那套已被推翻、代码也不再被调用，**别照着做**。原因见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 文末。

---

## 一、唯一约束

> **每个事实只有一个写入者。Apple 应用是状态源；agent 只「增」，从不以自己为准去改或删。**

这一条推演出其余全部设计。犹豫时回到这句话。

| 存储 | agent 权限 | 你的权限 |
|---|---|---|
| 提醒事项 | 读（日报）+ **增** | 打钩、改字、**删** |
| Apple 日历 | 读（日报）+ **增** | 改时间、**删** |
| Apple 备忘录 | 读快照 + **增** | 改、**删** |
| `data/journal/` | 增（追加，永不修改） | 读 |

**「删除」全部在你手里。** 这消除了"agent 误删 / 复活你删掉的东西"整类问题。

### agent 没有"我的副本"

```
        ┌──────────────────┐
        │  Apple 应用       │ ← 状态源（唯一真相）
        └────────┬─────────┘
                 │ 只读快照（电压表并联在此）
                 ▼
            journal（读数记录，不是状态源）

   agent ──追加写(单向)──→ Apple 应用
     ↑                          │
     └──────── 不回读 ───────────┘
```

`journal` 是**传感器**，不是电源 —— 它没有"当前状态"这个概念。
"某条备忘还在不在"永远从 Apple 应用现读。

---

## 二、日常怎么用

**你只需要做两件事：发一句；在 App 里打钩。**

```
你 → Telegram 说一句
        │
        ├─ 待办 ──→ 提醒事项      「已记下（待办）：交电费」
        ├─ 日程 ──→ Apple 日历    「已记下（日程）：周五 14:00 项目周会」
        └─ 备忘 ──→ 备忘录（追加） 「已记下（备忘）」
        │
        └─ 原始输入 + agent 的决定 ──→ journal（可复盘）
                    │
                    ▼
   21:30  只读三处快照 → 日报 → Telegram（+ Bark 冗余）
```

| 说什么 | 去哪 |
|---|---|
| 「明天交电费」 | 提醒事项 |
| 「周五下午两点项目周会」 | 日历（带时间的自动归日历） |
| 「想起一件事，荷载按名称命名」 | 备忘录 |

**判不准时会给你三个按钮**（待办/日程/备忘），点一下才写 —— 机器不猜你的意图。
**绝不静默丢弃**：判不出来就归为最轻的归属（备忘），并如实回执。

### 四个概念

| 概念 | 判据 | 状态源 | 完成态 |
|---|---|---|---|
| **待办** | 一件"要做的动作" | 提醒事项 | ✅ 你打钩 |
| **日程** | "某时间点发生的事" | 日历 | ❌ 过去即结束 |
| **备忘** | 一条"记下来别忘了" | 备忘录 | ❌ 你删掉即结束 |
| **用户日志** | agent 观察到的读数 | `data/journal/` | — |

周期性事务（每周一交周报）**适合日历** —— 它有重复规则，而提醒事项的脚本接口不支持。

---

## 三、运维命令

```bash
cd /Users/carl-mini/dev/siri-carl/pdca

# 自检（455 项，全部离线，不碰你的数据、不需要授权）
python3 src/selftest.py

# 体检：配置 + 三处授权 + 通道 + 任务状态（只读）
bash deploy/install_launchd.sh doctor

# 任务状态（含上次退出码）
bash deploy/install_launchd.sh status

# 安装 / 重新加载 / 卸载
bash deploy/install_launchd.sh install
bash deploy/install_launchd.sh restart     # 装过但没在跑时用
bash deploy/install_launchd.sh uninstall

# 手动验证链路（不推送、不写入备忘录）
bash deploy/install_launchd.sh test

# 日报（只看内容，不推送）
python3 src/report.py --no-push
```

> ⚠️ **`daemon.py --offline` 不等于"隔离测试"。**
> 它只保证**不写入任何 Apple 应用**（回执里告诉你"会写到哪里"），
> 但**仍然连 Telegram 并推进 offset**。而守护正在跑时也在读同一批更新 ——
> 两个进程会争抢，你在测试窗口里发的消息可能被测试进程吃掉、不回执。
>
> **这是实测到的**，不是推测：跑一次 `--offline --once`，守护日志立刻出现
> ```
> 拉取失败：HTTP 409：{"ok":false,"error_code":409,
>             "description":"Conflict: terminated by other getUpdates request
> ```
> 守护没崩（`run_once` 吞掉后 5 秒重试，这正是它该做的事），
> 但那个窗口里你发的消息有真实概率丢失。
>
> 想安全试解析逻辑，用 `python3 src/selftest.py`（完全离线，不碰网络与数据）；
> 真要观察离线回执，先 `install_launchd.sh uninstall` 停掉守护再跑。

**两个任务的分工**（刻意分开：写入路径只有一个入口，出问题排查面小得多）：

| 任务 | 时间 | 是否写数据 |
|---|---|---|
| `com.carl.pdca.daemon` | 常驻（KeepAlive） | ✅ 唯一写入者 |
| `com.carl.pdca.report` | 每天 21:30 | ❌ **只读** |

v1 的 `com.carl.pdca.sync` / `com.carl.pdca.carryover` 会在安装时被自动清理 ——
它们指向已废弃的脚本，留着会"两套系统同时在跑"。

### 真实环境自检

```bash
python3 tools/selftest-live.py    # 真的去读三处 App（需要授权）
python3 tools/smoke.py            # 端到端冒烟
python3 tools/probe-native-dates.py  # 只读探测：Apple 原生日期能力（见坑 16）
```

---

## 四、目录

```
pdca/
├── config.json                三处 App 的定位（folder id / 列表名 / 日历名）
├── .env                       Telegram token、Bark key（0600，已 gitignore）
│
├── data/                      ← 业务数据（要备份；丢了不可再生）
│   ├── journal/2026-10-03.jsonl   传感器读数（动作 + 观察）
│   ├── digest/2026-10-03.md       日报历史
│   └── pending/                   等你点按钮的待分类消息
│
├── logs/                      ← 程序日志（debug 用，可随时清）
│
├── src/
│   ├── telegram.py            双向通道（唯一能"提问"的通道）
│   ├── classify.py            分类：待办 / 日程 / 备忘
│   ├── whens.py               时间解析（全在 Python，避开 AppleScript 区域设置坑）
│   ├── intake.py              收件分发（写入端可注入 → 能彻底离线测试）
│   ├── daemon.py              收件守护（常驻，offset 落盘）
│   ├── report.py              日报（只读三处快照）
│   ├── reminders.py           提醒事项（只增）
│   ├── applecal.py            日历（只增）
│   ├── memo.py                备忘录（只读快照 + 追加）
│   ├── journal.py             传感器读数（追加不改）
│   ├── notify.py              Bark 冗余通道
│   └── selftest.py            455 项离线自检
│
├── deploy/
│   ├── setup-v4.sh            一键初始化
│   ├── install_launchd.sh     任务管理（install/status/test/restart/doctor）
│   ├── com.carl.pdca.*.plist  任务定义
│   └── probe-*.sh             能力探测脚本（历史存档）
│
├── tools/                     真实环境自检与冒烟
└── docs/
    ├── ARCHITECTURE.md        ← 架构定稿（要先看这个）
    ├── CONCEPT.md             最初的概念（部分已被 ARCHITECTURE 取代）
    └── ORIENTATION-ROOTCAUSE.md  照片方向问题排查（与 v4 无关，同属踩坑记录）
```

### 两类日志必须分开

| | `data/journal/`（用户日志） | `logs/`（程序日志） |
|---|---|---|
| 是什么 | 原始输入 + agent 的决定 | 程序跑了什么、报了什么错 |
| 给谁看 | **agent**（复盘） | **开发者**（debug） |
| 保留 | **永久**（丢了不可再生） | 有限（可随时清） |
| 备份 | ✅ 要 | ❌ 不必 |

程序日志里引用你的内容时会**截断**（只留前 30 字符）—— 因为 `logs/` 可能被贴出来排查问题。

---

## 五、失败与纠偏

| 情况 | 处理 | 后果 |
|---|---|---|
| Telegram 不可用 | **你直接在三个 App 里手动记** | agent 无需知晓，日报时读真相 |
| agent 解析错 | 你在对应 App 里改掉 | 同上 |
| 守护进程挂了 | 你手动记；`restart` 恢复后无状态依赖 | 只损失"一次自动整理" |
| 任何不一致 | **以 Apple 应用为准** | agent 无副本，不会"坚持己见" |

**核心性质：agent 不维护"我做过什么"的记忆用于决策。** 每次从 Apple 应用现读真相，
所以任何环节挂掉都只损失一次自动整理，**不会造成数据不一致**。

### 加新功能前的防掉坑判据

任一条命中，就说明又在造"有状态对话"：

```
✗ 需要跨两次交互记住东西（除了"这条的 id"）
✗ 需要重绘整条消息来体现进度
✗ 需要超时 / 重入 / 确认屏来处理"用户没按预期操作"
✗ 需要猜"这条是不是同一件事"
✗ 让 agent 拥有"删"权限
✗ agent 用自己写的东西作为下次写入的依据
✗ agent 在"要动你的数据"时不先回显确认
```

---

## 六、实测踩过的坑（都在代码里做了防线）

### 1. 「没权限」与「没数据」必须分得清
`SBApplication`（ScriptingBridge）在权限被拒时**静默返回空集合**，
让你以为你真的没有待办。所以备忘录通道改用 `NSAppleScript` —— 它会明确返回
`-10004`（越权）或 `-1743`（未授权）。同理，日报在未授权时**拒绝输出**
而不是给一份空摘要。

### 2. 必须用 id 定位，不能用名字
你的日志文件夹曾叫 `Note`，而系统原本就有 `Notes`（21 条真实笔记），
两个名字只差一个字母。配置里存的是 **id**，所以改名不影响。

### 3. 限定的查询，绝不用全局 `every note`
删除的备忘录会进「Recently Deleted」保留 30 天，而 `count of notes` 与
`every note` **仍然会返回它，且排在第一位**。所有查询都带 `of folder id "..."`。

### 4. 写入必须读回验证
备忘录存在「命令不报错但什么都没做」的静默失败。
只看有没有抛错会把失败当成功。

### 5. 走 `osascript` 子进程，而不是让程序自己操作备忘录
TCC 授权绑定调用进程身份。`osascript` 用的是**终端**的授权，稳定不变；
做成 app bundle 时 ad-hoc 签名每次重新编译哈希都会变，授权随之失效。

### 6. AppleScript 里不写日期字面量
`date "2026年10月5日 下午2:00:00"` **依赖系统区域设置**，换台机器或改个语言
就可能解析失败或差一天 —— 这类 bug 极难发现。所以时间语义全在 Python
（`whens.py`）算成整数，AppleScript 只负责机械赋值。

### 7. Telegram 长轮询会抛 `RemoteDisconnected`，它**不是** `URLError`
它是 `ConnectionResetError` → `OSError`。只捕 `URLError`/`HTTPError` 会漏网，
异常冒到守护导致进程退出。已单独兜住 `(HTTPException, ConnectionError, OSError)`。

### 8. `offset` 必须跨调用持久化
offset 只活在内存里的话，进程重启 → 重新拉到旧消息 → **同一条被记两遍**。
落盘在 `data/daemon-state.json`。

### 9. launchd 的返回码**不可信**
`launchctl load` 即使失败也返回 0。唯一可靠的判据是 `launchctl print` 能否查到。
**但「能查到」也≠「在跑」** —— 见下一条。

### 10. ⚠️ `bootout` 之后不能睡固定几秒就 `bootstrap`
这是"安装脚本报成功、发消息却没人接"的**真根因**，修了两次才看清：

守护在长轮询里（最长 25 秒），收到 SIGTERM 要等这次长轮询返回才真正退出。
`bootout` 后只睡 1 秒就 `bootstrap`，新任务刚起来、旧进程才退出 ——
launchd 把这次退出算在新任务头上，新任务立刻变成 `state = SIGTERMed`，
**旧的停了、新的也没了**，而脚本打印的是 ✅。

两处修正：
1. `wait_job_gone`：等任务**真的从 launchd 域消失**（`print` 查不到）再加载，上限 40 秒；
2. `verify_loaded`：加载后**等 3 秒再验存活**，且判据分任务类型 ——
   守护常驻必须 `running`，日报是定时任务、平时本来就 `not running`
   （对它断言 running 会得到一条永远失败的假告警）。

### 11. `KeepAlive` 用字典写法，不用 `<true/>`
`<true/>` 的语义是"**非正常退出**就重启"。守护收到 SIGTERM 后若 `return 0`，
launchd 认为"任务完成了"，从此不再拉起 —— 表现是"✅ 已加载"但 `state = SIGTERMed`、
没有活进程。改用 `<dict><key>SuccessfulExit</key><false/></dict>`，语义无歧义。

### 12. plist 注释里不能出现 `--`
XML 注释不允许连续两个连字符。`plutil -lint` 用宽松解析器**会放过**这个错误，
而 launchd 拒绝加载 —— 表现是"安装显示成功、任务却不在跑"。
自检里用 `plistlib`（严格解析）逐个验证，并单独扫注释里的 `--`。

### 13. bash 3.2 会把全角字符的字节当成变量名的一部分
```bash
echo "删除当天页：「$TARGET」"     # → TARGET」: unbound variable
```
`」` 紧跟 `$TARGET`，bash 就去找名为 `TARGET」` 的变量，`set -u` 于是报未定义，
**报错信息还把变量名截断**，看不出真相。

**规矩：`$VAR` 后面要跟中文/全角标点，一律写 `${VAR}`。**
自检固化了一项检查，扫描 `deploy/*.sh` 报这类写法。

### 14. 疑似重复只报告，绝不自动删
相似度高不等于重复（"提交结算单A"和"提交结算单B"可能是两件事）。
静默去重会丢数据，而丢数据是看不出来的。

### 15. 留档独立于 Apple 应用
Apple 应用是书写界面，不是数据库。它可能因授权失效、iCloud 不同步、误删而读不到。
journal 每天独立留痕，出问题时你能看见丢了什么。

### 16. 「读全量再在 Python 里筛」会悄悄出错 —— 能用原生条件就用原生条件

日报的"今日完成"原来取**全量**提醒事项、只按 `completed` 布尔值分流，
于是昨天、上个月完成的条目**全都堆在"今日完成"里**，日报越看越不可信。

判据本来就在 Apple 那边：`completion date` 是每条提醒事项的原生属性。
探测（`tools/probe-native-dates.py`）实测：

| 探测项 | 结果 |
|---|---|
| 已完成条目的 `completion date` | ✅ 读得到真实时刻 |
| `whose completion date is greater than d` | ✅ 服务端可过滤 |
| 未完成条目的该字段 | `missing value`（正好可作判据） |

**同理，解析出来的日期不要让 AppleScript 回字符串**：
"2026年10月3日 星期六 下午2:13:30" 依赖系统区域设置，
换台机器/改个语言就可能解析失败或差一天（`applecal.py` 顶部有同源教训）。
所以回"年,月,日,时,分"**整数分量**，由 Python 组装。

### 17. 「没读到」和「被删了」是两件事 —— 混淆会静默清空数据

备忘的"防遗忘"要在日报里提醒"放了 N 天还没处理"的条目，
而"处理完了"的信号是**你在备忘录里删掉它**。
探测证实备忘录**没有**原生删除线索（删除只是移进 `Recently Deleted` 保留 30 天），
所以只能靠**只读快照差集**：台账里有、快照里没有 → 你删了 → 记 `memo_cleared`。

**关键在失败分支**：快照读不到时**绝不能**当成"都被删了"——
那会静默清空全部提醒，而你看不出来。所以读不到就退化成"照旧提醒"
（宁可多提醒一条，也不要假装你处理完了）。

这条路径曾在架构文档里写得很完整，却**几个月没有生产代码接上**，
而日报页脚一直写着"删掉即可，之后不再提醒"——**承诺了没实现的功能**。
教训：文档里写的路径，要有断言盯着它真的接上了（见 `selftest` 的"感知路径"一节）。

---

## 七、授权（出问题先看这里）

三处 App 的授权互相独立，「日历给了、备忘录没给」很常见。
`deploy/install_launchd.sh doctor` 会**真实读取**每一处并如实报告。

授权挂在**调用进程的身份**上。用终端跑脚本时，授给的是终端：

```
系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「提醒事项」「日历」「备忘录」
```

**实测提醒**：在受限沙箱里跑（例如某些自动化环境），会得到 `-10004 越权`，
但这**不代表**你机器上的真实授权有问题 —— 换到终端或守护进程里就是好的。
判断依据要认 `doctor` 的输出，不要认沙箱里的报错。

---

## 八、下一步（待实现）

> **改动代码后请先跑 `python3 src/selftest.py`。**
> 其中有一项静态契约检查：确认上层调用的每个方法都真实存在。
> 这条检查是因为一次重构误删了 `names()` 和 `note_id_for()`，
> 而它们只在真正读备忘录时才被调用 —— 沙箱里跑不到，错误一路流到用户那里才暴露。

| 优先级 | 功能 | 说明 |
|---|---|---|
| P1 | 周报 | 完成率、连续天数（等一两周真实数据后再做） |
| P2 | 守护的可观测性 | 现在只能看日志；想要"守护是否健康"的一句话结论 |
| P3 | 智能体诊断 | 任务失败时交给 headless 智能体先定位，搞不定才推给你 |
| — | 清理 v1 遗留代码 | 见下 |

### v1 遗留代码：已标注，**不要顺手删 `parse.py`**

`src/` 下有 11 个文件带着「⚠️ v1 遗留代码 —— v4 不调用本文件」的头部注释：

```
carry_over.py  cleanup.py  cleanup_reminders.py  completion.py
daily_report.py  notes.py  probe_reminders.py  push_tasks.py
read_day.py  reconcile.py  sync.py
```

**`parse.py` 不在这个名单里，虽然它看起来最像 v1。**
它是**活代码**：[`src/reminders.py:292`](src/reminders.py#L292) 会 import 它，
所以删掉会让 v4 的提醒事项功能直接坏掉。判断"哪些是死代码"不能靠文件名或文档，
要从 deploy 的入口（`daemon.py` / `report.py`）顺着 import 算可达性 ——
我就是先按文档列名单、算出依赖后才发现 `parse.py` 是活的。

想自己重算：

```bash
cd src && python3 -c "
import re,pathlib
SRC=pathlib.Path('.')
deps=lambda f:{m.group(1)+'.py' for m in re.finditer(r'^\s*(?:import|from)\s+([a-zA-Z_]\w*)',(SRC/f).read_text(),re.M)} & {p.name for p in SRC.glob('*.py')}
seen,stack=set(),['daemon.py','report.py']
while stack:
    c=stack.pop()
    if c in seen: continue
    seen.add(c); stack+=list(deps(c)-seen)
print('活代码:',sorted(seen))
print('遗留候选:',sorted({p.name for p in SRC.glob('*.py')}-seen-{'selftest.py'}))
"
```
