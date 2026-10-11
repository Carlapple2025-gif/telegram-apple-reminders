---
name: apple-app-automation
description: Automate macOS Calendar, Reminders, and Notes with AppleScript or osascript — what each app can and cannot do, and the measured cost of each read. Use when writing or debugging AppleScript for these apps, when a script silently writes nothing, when reads return empty, when "it should remind me" but nothing fires, when a calendar created by script does not appear on iPhone, when a date is off by a day, or before promising a recurring reminder.
---

# Apple 三件套的脚本自动化：能做什么、不能做什么、代价多少

**先探测，再实现。** macOS 的脚本接口里，"文档说支持"和"实际能用"经常不是一回事 ——
写入静默失败、`whose` 缺运算符编译失败、权限被拒返回空集合，都是这么发现的。
`scripts/probe-apple-capabilities.py` 是只读探针（一个字节都不写），先跑它再动手。

## 一、底模：日历和提醒事项不是"同一份数据的两个视图"

它们是**同一个库里两种不同的对象**。日历会把**带日期**的提醒事项**画**在格子上
（Apple 叫 Scheduled Reminders）—— 那是**只读叠加**；提醒事项**看不到**日历事件。
这条单行道是 2012 年把 to-do 从日历拆出去时定下的。

| 你想要的 | 能不能 | 依据 |
|---|---|---|
| 待办有"做完 / 没做完" | ✅ **只有提醒事项有** | `reminder` 有 `completed` / `completion date` |
| 重复（每天 / 每周） | ✅ **只有日历有**，且仅在脚本层 | `reminder` 的属性表里**没有** `recurrence` |
| 到点弹提醒 | ✅ 单次两边都能设；**重复**只有日历 | `remind me date`（单次）/ `display alarm`（日历） |
| 日历上看得见 **且** 能打钩 | ❌ Apple 的模型里没有对应物 | `EKEvent` 没有任何完成态字段 |
| 日历事件自动变成待办 | ❌ 没有这条通道 | 历史被拆掉了 |
| 提醒事项自动出现在日历 | ⚠️ 仅当它**有到期日**时 | 只读叠加 |

对象的完整属性表以**脚本字典**为权威，别背：

```bash
grep -o '<class name="[^"]*"' /System/Applications/Calendar.app/Contents/Resources/iCal.sdef
grep -o '<class name="[^"]*"' /System/Applications/Reminders.app/Contents/Resources/Reminders.sdef
```

提醒事项的 `reminder` 一共 13 个属性：`name` `id` `container` `creation date`
`modification date` `body` `completed` `completion date` `due date` `allday due date`
`remind me date` `priority` `flagged`。只有前 4 个只读。

**"已编排 / 今天 / 已加上旗标"不是字段**，是智能列表 —— 设了日期、或 `flagged` 为真，
就自动进去。别去找"设置它"的接口。

## 二、硬约束（每条都实测过，且带后果）

### 1. 重复提醒建不出来 → 周期性事务只能放日历

`Reminders.sdef` 里 `reminder` **没有 `recurrence`**。**但这不是提醒事项的天生缺陷**：
`EKCalendarItem`（`EKEvent` 和 `EKReminder` 的共同基类）**有** `recurrenceRules` ——
缺的是脚本层的暴露。所以这条约束**只依赖脚本字典**：哪天 Apple 把 `recurrence`
加进 `Reminders.sdef`，约束就消失了，而探测会立刻显示出来。

> 探测时**别用写入去验证**这一条：那会在用户数据里留一条重复提醒。

### 2. "日历上看得见 + 能打钩"没有对应物

`EKEvent` 里**没有任何完成态字段**。想要这种条目，说明你要的其实是提醒事项
（并接受它会因为带日期而出现在日历上）。别试图用 `event` 模拟。

### 3. 脚本建不了 iCloud 日历 —— 新日历会落在「我的 Mac 上」

日历的脚本字典里**没有 `source` / `account` 这一类对象**（对象类型只有
`attendee` `calendar` `display alarm` `event` `mail alarm` `open file alarm` `sound alarm`）。
于是 `make new calendar with properties {name:"X"}` **指定不了账户**，
落在哪儿由日历 App 自己决定 —— 实测落到了**本地账户**。

**后果**：本地日历**不进 iCloud**，只有这台 Mac 看得见；iCloud 同步开关开着也没用
（那个日历从没上传过）。

**判别（10 秒）**：日历 App 左侧栏找到那个日历名 → 看它在**哪个分组标题**下。
「我的 Mac 上」= 本地；「iCloud」= 会同步。
⚠️ 这与"iPhone 设置里 iCloud 日历开关"是**两件事**。

**做法**：复用 iCloud 下**已有**的日历（最省事），或在 iPhone / icloud.com 上新建，
然后让脚本写进它。**别用脚本建日历。**

⚠️ **这条不要"探测"**：`make new calendar` 成功一次就真的多出一个假日历 ——
而那正是要防的事。

### 4. 读"完成历史"是逐条属性访问，约 1.4 秒/条

实测（默认列表 7 条）：

| 读法 | 耗时 | 折算 |
|---|---|---|
| 原生筛计数（`whose completed is false / true`）| 1194 ms | 固定开销 ~1.2 s |
| 全量读（7 条，回 Python 筛）| 2150 ms | ~0.3 s/条 |
| **读已完成明细（5 条，含 `completion date`）** | **6983 ms** | **~1.4 s/条** |
| **窗口筛**（`completed is true and completion date ≥ current date - 3 * days`）| **595 ms** | 比不筛的计数还快 |

> 最后一行看着反直觉（筛了反而更快），原因是**要看多少条**：不筛的计数要遍历
> 全列表 7 条，窗口里只有 2 条。**代价由命中条数决定，不由条件复杂度决定** ——
> 这与下一条「1.4 s/条」是同一个道理。

**代价随历史条数线性增长**：7 条 7 秒，100 条就是 2 分钟以上。
所以：**不能每晚读全量历史** → 只读最近几天的**窗口**；更长的统计
（周 / 月达成率）**从自己的留档算**，不从 Apple 读。
读回来的**立刻留档** —— 这样用户之后"清除已完成"也不丢数据。

### 5. 一条重复事件是**一个**对象

`start date` 是它的**首次**发生日。所以窗口查询**只能查到它首次那天**，
之后每次发生要靠重复规则**自己展开**。不展开的话，`@每天八点 跑步`
从第二天起就会在列表和日报里**消失**（实测踩过）。

⚠️ `whose recurrence is not missing value` **实测报 -1700**，别用它筛重复事件。

## 三、写 AppleScript 时的坑

### ① `whose` 子句一律写全运算符

`whose id "X"` / `whose completed false` **缺 `is` 会编译失败**（-2741）。
写 `whose id is "X"`、`whose completed is false`、
`whose completion date is greater than d`。

⚠️ 这里有个**上下文差异**，别被它误导：某些上下文里 `>` 能用，但
`is greater than` 永远安全。在源码里固化成一条静态检查更省事 ——
**每个 `whose` 子句附近必须有 `is` / `contains`**，把这条写成断言，
比每次靠记忆强。

### ② 绝不写日期字符串字面量

`date "2026年10月5日 下午2:00:00"` **依赖系统区域设置** —— 换台机器或改个语言，
就可能解析失败或**差一天**，而这种 bug 极难发现。

**规矩**：日期语义全在你的语言里算（Python 等），AppleScript 只做**机械赋值**：

```applescript
set dFrom to current date
set year of dFrom to 2026
set month of dFrom to 10
set day of dFrom to 5
set hours of dFrom to 14
set minutes of dFrom to 0
set seconds of dFrom to 0
```

**读**日期同理：不要 AppleScript 回字符串，回**整数分量**
（`year of cd as integer`、`month of cd as integer`、`day of cd` …），由你的语言组装。

> 这不与"能用原生条件就用原生条件"冲突：`whose start date ≥ dFrom` 比较的是
> **两个绝对时间**，全程不经过字符串解析 —— 那才是对的用法。

### ③ 「没权限」和「没数据」必须分得清

脚本桥（ScriptingBridge / `SBApplication`）在权限被拒时**静默返回空集合** ——
让程序以为"你真的没有待办"。所以要**走 `osascript` 子进程**，它会明确报错：

| 错误码 | 含义 |
|---|---|
| `-10004` | 越权（privilege violation）—— 多为沙箱 / 未在自动化里勾选 |
| `-1743` | 系统不允许发送 Apple 事件 —— 同上，需授权 |

**授权挂在"调用进程"的身份上**（TCC）。用终端跑脚本时，授给的是**终端**：
系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选对应 App。

**这是选 `osascript` 子进程而不是 PyObjC 的真正理由**：做成 app bundle 后，
ad-hoc 签名每次重新编译哈希都变，授权随之失效；而终端的授权稳定不变。

⚠️ **沙箱里跑出 `-10004` 不代表这台机器的真实授权有问题** —— 换到终端或
守护进程里就是好的。判断依据要认明确的授权检查输出，不要认沙箱里的报错。

**推论**：读不到数据时**绝不能**当成"都被删了"或"今天没有"。
宁可如实报"读取失败"，也不要给一份空摘要 —— 后者会静默清空全部提醒，
而用户看不出来。

### ④ 写入必须读回验证

这三个 App（尤其备忘录）存在**"命令不报错但什么都没做"的静默失败**。
只看有没有抛错，会把失败当成功。

做法：写完按 **id** 查回，**逐字段比对**（`flagged`、`priority`、`completed`、
`due date` 都要比），不一致就报错。别用标题比对 —— 标题可能被规范化。

### ⑤ 全天事件的闹钟不能用 `interval 0`

全天事件在日历里的"事件时刻"是当天 **00:00**，`trigger interval: 0`
会在**半夜**弹。用户看不出那是 bug，只会觉得吵。给全天事件用一个明确的钟点
（比如当天 09:00 = `540` 分钟）。

### ⑥ 定位一律用 id，且绝不用全局查询

这两套 App 里**名字不是唯一标识**，而且各自还分账户：

- 配置里存 **id**，不存名字（用户的备忘文件夹曾叫 `Note`，而系统本来就有 `Notes`，
  两个名字只差一个字母）；
- **删除的备忘录会进「Recently Deleted」保留 30 天**，而
  `count of notes` 与 `every note` **仍然会返回它，且排在第一位** ——
  所有查询都要带 `of folder id "..."` 限定；
- 别留两个同名日历：`calendar "X"` 按名字找，同名**会有歧义**（命中哪个不确定）。

**诊断"看不到 / 找不到"时，先问一句：它在哪个账户下？**

## 四、参考实现

`scripts/probe-apple-capabilities.py` —— 只读探针，把上面每条约束都测一遍：
权限、脚本字典、`whose` 三种写法、提醒事项的读取代价、日历窗口查询。

```bash
python3 scripts/probe-apple-capabilities.py --quick     # 只测权限 + 字典（秒级）
python3 scripts/probe-apple-capabilities.py             # 全跑（会读真实数据）
```

**不探测的两件事**，以及为什么：建 iCloud 日历（写下去就多个假日历）、
闹钟与重复规则的写入行为（要写用户数据）。这两条的判别方法见上文。

## 五、怎么"改结论"

平台事实**带版本**（本文核对于 macOS 27.0.1）。换大版本、或升级后行为与本文不符，
**先跑一遍探针再改结论** —— 不要凭印象改。

**改的是结论，别改证据。** 证据是命令的输出；它变了就把新输出贴进来、写清哪天哪版，
再改结论 —— 这样下一个人能看出"是平台变了"还是"我们记错了"。

## 边界

**这个 skill 记的是平台约束，不是设计选择。** 判断方法：**"换成另一个项目还成立吗？"**

以下**不**属于本 skill（它们是具体项目的设计决定）：谁是唯一写入者、
哪些通道只读、符号怎么声明、用户日志留什么字段、定时任务几点跑。
那些应该留在**那个项目的架构文档**里。

---

来源：`pdca`（telegram-apple-reminders）在 macOS 27.0.1 上实测所得，
证据与复核命令原本记在该项目的平台事实文档里。
