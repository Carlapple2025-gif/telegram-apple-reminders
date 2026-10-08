# telegram-apple-reminders

> **Telegram in, Apple apps out.** Send one message to a Telegram bot and it lands in
> the right Apple app on your Mac — **Reminders**, **Calendar** or **Notes**.
> A daily review is pushed back at 21:30. No database, no sync:
> **your Apple apps stay the single source of truth.**
>
> Python + AppleScript + launchd · self-hosted · single user · macOS.
> You declare the type with a leading symbol (`#` memo · `@` calendar · `!` flag ·
> nothing = to-do) — the code **never guesses** what you meant. That was the v1 design,
> and it was thrown away ([why](CHANGELOG.md)).
>
> 中文文档在下面。使用者视角见 [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md)；
> 系统怎么工作见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

### Quick start (English)

**Requirements** — a Mac that stays on (a Mac mini is ideal), Python 3 (the system
`/usr/bin/python3` is enough: stdlib only, no venv to break), a Telegram bot, and
Automation permission for Reminders / Calendar / Notes.

```bash
# 1. a dedicated bot: talk to @BotFather → /newbot → copy the token
git clone https://github.com/Carlapple2025-gif/telegram-apple-reminders.git
cd telegram-apple-reminders

# 2. config
cp .env.example .env && chmod 600 .env
#    put TELEGRAM_BOT_TOKEN in .env, then ask the bot for your chat id:
python3 src/telegram.py whoami

# 3. find or create the Apple containers (memo folder + calendar). Idempotent.
bash deploy/setup-v4.sh                                        # dry run, changes nothing
bash deploy/setup-v4.sh --apply --memo-folder=Memo --calendar=Assistant

# 4. grant Automation (System Settings → Privacy & Security → Automation),
#    then let it really read all three apps and report honestly:
bash deploy/install_launchd.sh doctor

# 5. install the three launchd jobs: daemon + 21:30 digest + Sun 20:00 weekly
bash deploy/install_launchd.sh install
```

**What you send** — the leading character declares the type. The code never guesses:

| you send | it lands in |
|---|---|
| `交电费` / `pay the bill` | Reminders — **bare text is always a to-do** (it goes to your default list) |
| `!pay the bill` | Reminders, **flagged** (shows up in *Flagged*) |
| `!!pay the bill` | Reminders, flagged + **high priority** |
| `明天交电费` | to-do **with a native due date** — *Today* / *Scheduled* start working |
| `@明天下午两点 项目周会` | Calendar (`@` requires a time it can read, otherwise it errors and writes nothing) |
| `# 学原理比学语法重要` | Notes (appended) |
| `/list` | read-only: open to-dos + today/tomorrow's events |

**Verify it works** — everything is checkable without touching your data:

```bash
python3 src/selftest.py                  # 852 assertions, fully offline
python3 src/intake.py --dry "明天交电费"  # dry run: routing + receipt, writes nothing
bash deploy/install_launchd.sh status    # are the three jobs alive?
```

**Design in one line:** no database, no sync — Reminders / Calendar / Notes stay the
single source of truth, and the agent only ever **adds**; it never edits or deletes.
The docs themselves are in Chinese: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the
design authority, and [`CHANGELOG.md`](CHANGELOG.md) records what was built, thrown away
and why (v1 guessed your intent with a 700-word dictionary — it was deleted).

**用一句话说**：你对 Telegram 发一条（用行首符号说明是待办 / 日程 / 备忘），
Mac mini 把它写进对应的 Apple 应用（提醒事项 / 日历 / 备忘录），21:30 自动生成日报推回来。

**当前状态：v4.1，守护与日报都在跑。** 你在 Apple 应用里打钩 / 改字 / 删除 —— 那才是唯一状态源。

> ⚠️ **符号就这么几个**：什么都不加 = 待办，`#` = 备忘，`@` = 日历，
> `!` = 给待办打旗标，`!!` = 旗标 + 高优先级。
> 要**看**现在有什么，发 `/list`（只读查询，一个字都不写）。
> v4.1（2026-10-03）之前是"发自然语言、代码猜类型"，那套已整体删除 ——
> 原因见 [`CHANGELOG.md`](CHANGELOG.md) 的 v4.1 一节。

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

| 你发的 | 落到 | 说明 |
|---|---|---|
| `交电费` | 提醒事项 | **什么都不加 = 待办**（最高频，零符号） |
| `!交电费` | 提醒事项 | `!` = 旗标（进"已加上旗标"）|
| `!!交电费` | 提醒事项 | `!!` = 旗标 + 高优先级 |
| `# 荷载按名称命名` | 备忘录 | `#` = 备忘 |
| `@周五下午两点 项目周会` | 日历 | `@` = 日历，**必须带能认出来的时间** |
| `- [ ] 交电费` | 提醒事项 | 复选框写法也可以（习惯用） |
| `/list` | **一个都不落** | **只读查询**：未完成的待办 + 今天/明天的日程（别名 `/ls` `/today` `/列表`）|

**类型由你写的符号决定，代码不猜。** 见
[`docs/SYMBOL-SCHEME.md`](docs/SYMBOL-SCHEME.md) 与 [`CHANGELOG.md`](CHANGELOG.md) 的 v4.1。

> `/list` 是唯一**不写任何东西**的输入。判据同样是**行首字符**（`/`）——
> 所以 `list` 不带斜杠就是一条待办，而不是命令。见 [`src/commands.py`](src/commands.py)。

### 报错而不是追问

缺硬信息时**只报错、不写入**（不退回别的类型、不猜一个时间）：

| 情况 | 回执 |
|---|---|
| `@` 后面没有能认出来的时间 | `❌ 没记成：这条日程的时间我认不出来…` |
| `@` 只有时间、没有事由 | `❌ 没记成：这条日程只有时间、没有写是什么事…` |
| 只有符号、没有内容（如单独一个 `#`） | `❌ 没记成：只有符号、没有内容` |

**不追问**是有意的：追问要跨消息记住"在等什么"，而那正是
2026-10-03 两次故障的所在地（见 `ARCHITECTURE.md` §十一 判据 1）。
缺什么就**带符号重发一条完整的**。

### 四个概念

| 概念 | 判据 | 状态源 | 完成态 |
|---|---|---|---|
| **待办** | 一件"要做的动作" | 提醒事项 | ✅ 你打钩 |
| **日程** | "某时间点发生的事" | 日历 | ❌ 过去即结束 |
| **备忘** | 一条"记下来别忘了" | 备忘录 | ❌ 你删掉即结束 |
| **用户日志** | agent 观察到的读数 | `data/journal/` | — |

周期性事务（`@每周一 交周报`）走日历 —— 它有重复规则，而提醒事项的脚本接口不支持。

---

## 三、运维命令

```bash
cd /path/to/telegram-apple-reminders    # 换成你 clone 的位置

# 自检（513 项，全部离线，不碰你的数据、不需要授权）
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

# 周报（周日 20:00 那一趟；只看内容，不推送）
python3 src/report.py --weekly --no-push

# Telegram 通道（独立 bot）
python3 src/telegram.py status             # token / chat_id / bot 名
python3 src/telegram.py menu               # 查看「/」菜单（打 / 时的自动补全）
python3 src/telegram.py menu --set         # 把 commands.MENU 推上去
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

**三个任务的分工**（刻意分开：写入路径只有一个入口，出问题排查面小得多）：

| 任务 | 时间 | 是否写数据 |
|---|---|---|
| `io.github.carlapple2025.pdca.daemon` | 常驻（KeepAlive） | ✅ 唯一写入者 |
| `io.github.carlapple2025.pdca.report` | 每天 21:30 | ❌ **只读** |
| `io.github.carlapple2025.pdca.weekly` | 周日 20:00 | ❌ **只读**（周报：完成 / 提交 / 连续天数） |

v1 的 `…pdca.sync` / `…pdca.carryover` 会在安装时被自动清理 ——
它们指向已废弃的脚本，留着会"两套系统同时在跑"。

**2026-10-07 改了标签前缀**（原来带个人名，现统一成 `io.github.carlapple2025.pdca.*`）。
`install` 会先卸掉**改名前的旧标签**再装新的 —— 这一步不能省：
两套守护会抢同一个 Telegram offset，同一条消息被处理两遍。

### 真实环境自检

```bash
python3 tools/selftest-live.py    # 真的去读三处 App（需要授权）
python3 tools/smoke.py            # 端到端冒烟
python3 tools/probe-native-dates.py  # 只读探测：Apple 原生日期能力（见坑 16）
python3 src/notify.py --status    # 两个通道 + 心跳的配置状态
python3 src/notify.py --heartbeat ok   # 只测心跳（fail 可测"上报失败"那条路）
```

### 投递与监控：怎么知道"日报没跑"

**两个通道（Telegram / Bark）只能证明"送到了"，证明不了"跑了"。**
机器睡过 21:30、任务被清掉、脚本在推送前崩掉 —— 这三种在本机都看不出来
（`launchctl print` 的计数会因 reload 归零，实测 `runs = 0 / never exited`）。

所以配一个**机器之外**的心跳（见 `.env.example` 的 `HEALTHCHECK_URL`）：

| 情况 | 心跳 | 你看到 |
|---|---|---|
| 日报完整送达 | ping 主 URL | 无消息（正常） |
| 数据不全 / 全通道失败 | ping `/fail` | 立刻告警 |
| **根本没跑** | **没有 ping** | 到点后由远端告警 ← 只有这条能发现"没跑" |

**没配心跳时**（不注册第三方服务也是一种正式选择），本机还有两个机制兜住：

| 机制 | 管什么 | 怎么用 |
|---|---|---|
| **看门狗**（跑在守护进程里） | 当天 23:30 还没有投递成功的读数 → 发一次告警（每天最多一次） | 自动；`python3 src/watchdog.py --check` 可随时看判断 |
| **日报的 gap 行** | 事后对账："⚠️ 上一份日报是 3 天前，中间漏了 2 次" | 自动出现在日报里（机器整晚没醒时只有它能说话） |

两者都**不加状态文件**：判断全部来自 `data/journal/` 的投递读数。

配套的两处留痕（都不依赖心跳）：

- 日报正文第一行是 **`🕘 生成于 HH:MM`**，偏离计划时间超过 30 分钟标题会带标记：
  **晚**了 →「（延迟）」；**早**了 →「（非计划时间）」。两种都源自 launchd 的语义
  —— `man launchd.plist`：睡过的定时任务会在**唤醒时补跑**且多次合并为一次。
  所以要防的不止"晚报"：睡过 21:30、凌晨才醒，跑出来的是**次日**那份空日报，
  而昨天那份永远不会来了 —— 这一句会明说。
- 每次投递的结果（每通道成败 / 是否延迟 / 心跳结果）**追加进 `data/journal/`**
  的 `digest_pushed` 记录 —— `logs/` 可以随时清，这里不能。
  某条通道连续 3 天没成功，日报末尾会自己说出来（否则单通道静默失效
  能瞒你几个月）。

---

## 四、目录

```
pdca/
├── CHANGELOG.md               ← 行为变化与架构决定（新会话先看这个和 PROJECT-STATE）
├── config.json                三处 App 的定位（folder id / 列表名 / 日历名）
├── .env                       Telegram token、Bark key（0600，已 gitignore）
│
├── data/                      ← 业务数据（要备份；丢了不可再生）
│   ├── journal/2026-10-03.jsonl   传感器读数（动作 + 观察）
│   ├── digest/2026-10-03.md       日报历史
│   ├── digest/week-2026-10-05.md  周报历史（按**周一**命名）
│
├── logs/                      ← 程序日志（debug 用，可随时清）
│
├── src/
│   ├── telegram.py            双向通道（唯一能"提问"的通道）
│   ├── routes.py              ★ 按行首符号决定归属（唯一决定落点的地方）
│   ├── commands.py            ★ `/list` 只读查询（唯一不写东西的入口）
│   ├── kinds.py               Kind / Item 数据结构
│   ├── whens.py               时间解析（全在 Python，避开 AppleScript 区域设置坑）
│   ├── intake.py              收件分发（写入端可注入 → 能彻底离线测试）
│   ├── daemon.py              收件守护（常驻，offset 落盘）
│   ├── report.py              日报（只读三处快照）
│   ├── reminders.py           提醒事项（只增）
│   ├── applecal.py            日历（只增）
│   ├── memo.py                备忘录（只读快照 + 追加）
│   ├── journal.py             传感器读数（追加不改）
│   ├── notify.py              Bark 冗余通道 + 可选的对外心跳
│   ├── watchdog.py            日报看门狗（守护里查"今天送出没有"）
│   └── selftest.py            513 项离线自检
│
│   ⚠️ 另有 11 个 v1 遗留模块也在这个目录里（carry_over / notes / sync /
│      completion / reconcile / push_tasks / read_day / daily_report /
│      cleanup / cleanup_reminders / probe_reminders），每个文件头都有
│      「⚠️ v1 遗留代码」标注。
│      **它们没有被挪进 legacy/，是有原因的**：这 11 个互相之间有 12 处
│      import 缠在一起，而 `parse.py` 被 v4 的 `reminders.py` 用着 ——
│      整体搬动要同时改 v4 模块的 import，收益不抵风险。
│      按文件头标注识别即可。
│
├── deploy/                    ← **只放"装机器"要用的东西**
│   ├── setup-v4.sh            一键初始化（干跑 / --apply）
│   ├── install_launchd.sh     任务管理（install/status/test/restart/doctor）
│   ├── io.github.carlapple2025.pdca.daemon.plist    常驻收件守护
│   ├── io.github.carlapple2025.pdca.report.plist    21:30 日报
│   ├── io.github.carlapple2025.pdca.weekly.plist    周日 20:00 周报
│   └── legacy/                v1 的两个定时任务定义（安装时会自动清理，
│                              留着是为了看清 v1 曾经装了什么）
│
├── tools/                     ← **只放"验与探"的东西**
│   ├── selftest-live.py       真实环境自检（需要授权）
│   ├── smoke.py               端到端冒烟
│   ├── probe_calendar.py      日历能力探测
│   ├── probe_memo_folder.py   备忘文件夹探测
│   ├── probe-native-dates.py  Apple 原生日期能力探测（见坑 16/17）
│   └── legacy/                v1 的探测与排查脚本（probe-*/init.sh/delete-day.sh…）
│
└── docs/
    ├── ARCHITECTURE.md        ← 架构定稿（唯一权威）
    ├── PROJECT-STATE.md       ← 现状与决定清单（新会话从这里接续）
    ├── SYMBOL-SCHEME.md       符号声明方案（**已实现**，保留作决策记录）
    ├── TELEGRAM-DESIGN.md     Telegram 层设计（已裁决）
    ├── USER-GUIDE.md          使用者视角（怎么用、说错了怎么收拾）
    ├── APPLE-FACTS.md         Apple 侧"能不能做"（日历/提醒事项，带可复核证据）
    ├── CONCEPT.md             最初的概念（部分已被 ARCHITECTURE 取代）
    ├── archive/               被推翻的 v1 架构（理解"为什么推翻"）
    └── ORIENTATION-ROOTCAUSE.md  照片方向问题排查（与 v4 无关，同属踩坑记录）
```

### 判断"某个文件属于哪"的规则

| 目录 | 只放什么 | 判断方法 |
|---|---|---|
| `deploy/` | 装机器要用的（初始化、任务管理、任务定义） | "新机器上要跑它才能用起来吗？" |
| `tools/` | 验与探（自检、冒烟、能力探测） | "它是用来回答'能不能/对不对'的吗？" |
| `src/` | 运行时模块 | "守护/日报运行时会 import 它吗？" |
| `*/legacy/` | v1 的、v4 不调用的 | "从 deploy 入口算 import 可达性，到不了吗？" |
| `docs/archive/` | 被推翻的设计 | "它描述的是已经不做的事吗？" |

**归档不等于可以忘掉**：自检的静态扫描（全角陷阱、`whose is`）
**覆盖 legacy 目录** —— 否则"挪到 legacy"就成了让检查失效的捷径。

> **文档之间是什么关系**：`ARCHITECTURE.md` 是唯一权威；
> `PROJECT-STATE.md` 是跨会话的接续点（已定 / 待定 / 下一步）；
> `CHANGELOG.md` 记"什么时候改了什么、为什么"。
> 看代码之前先看 `PROJECT-STATE.md`，能省掉重新推导的时间。

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
| ~~P1~~ | ~~周报~~ | **✅ 已实施**（2026-10-05，周日 20:00）：完成 / 提交 / 连续天数 |
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
