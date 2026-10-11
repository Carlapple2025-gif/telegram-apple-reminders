---
name: macos-launchd-jobs
description: Install, reload, and verify macOS launchd agents (daemons and scheduled jobs) so "it says success" means the job is actually alive. Use when writing a .plist, an install/restart script with launchctl bootstrap or bootout, or debugging "installed but not running", "worked after install then silently died", "launchctl returns 0 but nothing happens", or a KeepAlive job that never comes back.
---

# macOS launchd 任务：让"成功"是真的

`launchctl` 在这四件事上会**骗你**。每一条都实测踩过，表现在用户那侧是同一个症状：
**"装好了，但没人接"**。本 skill 给的是判据和验证顺序，不是背景介绍。

## 一、四条假成功

| # | 你以为 | 实际上 | 判据 |
|---|---|---|---|
| ① | `launchctl load` 返回 0 = 装上了 | **失败也返回 0** | 唯一可靠判据是 `launchctl print gui/$(id -u)/LABEL` **查得到** |
| ② | `bootout` 之后任务就没了 | **异步**。进程在长轮询里（最长 25 秒）要等这次调用返回才退 | 必须**轮询等它从域里消失**，不能睡固定秒数 |
| ③ | plist 文件在那儿 = 任务在跑 | launchd **只在 bootstrap 那一刻读 plist** | 文件在 ≠ 在册；在册 ≠ 在跑（③ 和 ④ 是两回事） |
| ④ | `bootstrap` 返回 0 且 `print` 查得到 = 成功了 | 旧的 SIGTERM 收尾会把**新任务一起带走**，几秒后它就没了 | **等 3 秒再验一次存活**，这是唯一可靠的判据 |

### ④ 的完整时间线（这是最难查的一条）

```
bootout 旧任务 ──→ 只睡 1 秒 ──→ bootstrap 新任务
                        │                    │
                        │                    └─ launchctl 报 0，print 也查得到 ✅
                        ▼
              旧进程这时才退出（长轮询刚返回）
                        │
                        ▼
        launchd 把这次退出**算在新任务头上** → 新任务 state = SIGTERMed
                        │
                        ▼
        脚本打印 ✅ 已加载 · 几秒后旧新都没了 · 用户发消息没人接
```

修过两次才看清。**下限**：等旧任务真消失（上限 40 秒，够覆盖一次 25 秒长轮询）

## 二、验证顺序（照这个顺序做，别跳）

```bash
# ① 校验 plist —— 用严格解析器，不要只信 plutil
scripts/launchd-job.sh check   /path/to/job.plist

# ② 装 + 加载 + 等 3 秒验存活（任何一步不过就报错，不打印假 ✅）
scripts/launchd-job.sh install /path/to/job.plist

# ③ 状态与上次退出码
scripts/launchd-job.sh status  /path/to/job.plist
```

`install` 内部就是 `bootout → wait_job_gone → bootstrap（重试 3 次）→ sleep 3 → verify_loaded`，
**失败时给的是重跑建议而不是 ✅**。`restart` 同理，用于"装过但没在跑"。

> ⚠️ **在受限环境里跑，先分清"权限"和"launchd"**：往 `~/Library/LaunchAgents` 写入
> 可能被沙箱或 TCC 拦掉，报 `Operation not permitted`。这时 `install` 会在**复制那一步**
> 就停下并直说"复制失败"，而不是滑到后面误报成"任务没加载 / 竞态" ——
> 两者的修法完全不同。**真正要验证安装，请在真实终端里跑**；
> 只读的 `check` / `status` 在受限环境里是准的。

### 存活验证必须按任务类型分开判

| 任务类型 | 期望 | 断言错了会怎样 |
|---|---|---|
| 常驻（`KeepAlive`） | **必须** `state = running` 且有 `pid` | 少了这条 = 发消息没人接 |
| 定时（`StartCalendarInterval`） | 只要**在册**就行，平时本来就 `not running` | 对它断言 running 会得到**一条永远失败的假告警** |

脚本从 plist 自己判断类型（读 `KeepAlive`），不需要你传参数、也无所谓标签叫什么。

## 三、写 plist 时的四个坑

1. **`KeepAlive` 用字典写法，别用 `<true/>`**
   `<true/>` 的语义是"**非正常退出**就重启"。进程收到 SIGTERM 后若 `return 0`，
   launchd 认为"任务完成了"，**从此不再拉起** —— 表现是"已加载"但
   `state = SIGTERMed`、没有活进程。字典写法语义无歧义：

   ```xml
   <key>KeepAlive</key>
   <dict><key>SuccessfulExit</key><false/></dict>
   <key>ThrottleInterval</key><integer>10</integer>
   ```

2. **XML 注释里不能出现连续两个连字符**（`--`）
   `plutil -lint` 用宽松的旧式解析器**会放过**，而 launchd **拒绝加载** ——
   表现又是"安装显示成功、任务不在跑"。写注释时把 `--drain` 改成 `—drain` 或加空格。
   **判据用 `plistlib`（严格解析），不是 `plutil`。**

3. **改名要"先卸旧的、再装新的"，且必须在同一次运行里完成**
   只装新的不够：两套同名不同的任务会**同时跑**，抢同一个外部资源
   （轮询 offset / 锁文件 / 端口），同一条输入被处理两遍。
   中间那个窗口就够出一次重复，所以两步之间不能有人工停顿。

4. **`RunAtLoad` + `StartCalendarInterval` 的组合要写清意图**
   定时任务若同时 `RunAtLoad`，每次安装都会先跑一次 —— 日报类任务会凭空多出一份。

## 四、环境隔离：让"没有环境可坏"

- **解释器固定用系统 `/usr/bin/python3`（或等价的系统自带运行时）+ 只依赖标准库。**
  理由不是洁癖：解释器链被清掉 / venv 被删 / 依赖升级，都会让任务在某天静默失效，
  而 launchd 只会记一个退出码。**系统解释器 + 标准库 = 没有环境可坏。**
- **`StandardOutPath` / `StandardErrorPath` 一定要给**，否则出问题时无处可看。
  同时明确：这类日志是**程序日志**（可随时清），跟业务数据分开存。
- **首次启动前先"排空"**：若任务会消费外部队列（消息、文件、offset），
  先跑一次 drain 型命令把积压处理掉并把游标定在最新之后，
  否则几天前的旧消息会在首次启动时被当成新输入重新处理一遍。
- **bash 3.2 的陷阱**：安装脚本里 `$VAR` 后面紧跟中文/全角标点时，
  bash 会把全角字节当成变量名的一部分（`TARGET」: unbound variable`，
  而报错还会把变量名截断，看不出真相）。**一律写 `${VAR}`。**

## 五、参考实现

`scripts/launchd-job.sh` —— `check` / `install` / `restart` / `status` / `uninstall` 五个命令，
自带上述四条判据，可直接拷贝到目标项目再按需改。它兼容 **bash 3.2**（macOS 自带），
不用关联数组、不用 `${var,,}`、不用 `mapfile`。

任务类型从 plist 自己推断（读 `KeepAlive`），因此脚本里**不出现任何具体项目的标签名** ——
如果你的项目要在脚本里硬编码标签，那正是"两处登记必然有一处是错的"的开始。

## 位置（为什么在这儿，改名前先看这一节）

本 skill 放在**项目根**的 `.dsh/skills/macos-launchd-jobs/`，因为技能发现是**按 rank 扫**的：

```
100  <projectRoot>/.dsh/skills          ← 本 skill（自动发现，跟仓库一起走）
200  <projectRoot>/.agents/skills
300  Config.customSkillDirs
400  ~/.dsh/skills
500  ~/.agents/skills
```

`projectRoot` = **最近的含 `.git` 的祖先目录**（本仓库里就是 `pdca/` 自己）。
两条推论：

- **嵌套的 `**/SKILL.md` 不会被发现** —— 放到 `pdca/skills/`（不带点）是扫不到的，
  必须是 `.dsh/skills/<名字>/SKILL.md` 这个形状，或者该根下的扁平文件 `<名字>.md`；
- 想放到别处就要配 `Config.customSkillDirs`（rank 300），
  代价是"多一处配置"，而配置与目录不一致时**不报错、只是加载不到**。

## 边界

**这个 skill 记的是平台约束，不是设计选择。** 平台约束换项目也成立；设计选择不成立。
以下属于**具体项目**的决定，别搬过来：任务该定在几点、谁是唯一写入者、
哪些任务只读、状态放哪儿。

判断方法：**"换成另一个项目还成立吗？"** —— 成立的是平台约束（留在本 skill），
不成立的是设计决定（留在那个项目的架构文档里）。

---

来源：`pdca`（telegram-apple-reminders）在 macOS 上运维三个 launchd 任务时实测所得，
判据在 `deploy/install_launchd.sh`、`src/selftest.py` 的 plist 一节里都有对应断言。
