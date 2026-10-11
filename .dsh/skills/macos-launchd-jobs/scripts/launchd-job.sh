#!/bin/bash
# launchd 任务的安装 / 重载 / 存活验证 —— 针对"脚本报成功、任务其实没跑"这一类故障。
#
# 为什么不能只调 launchctl 就完事（四条都实测过，见 ../SKILL.md）：
#   ① `launchctl load` / `bootstrap` **失败也可能返回 0**；
#   ② `bootout` 是**异步**的，旧进程可能还在跑（长轮询最长 25 秒）；
#   ③ launchd 只会在 bootstrap 那一刻读 plist，**文件在 ≠ 任务在跑**；
#   ④ 刚 bootstrap 完立刻 `print` 能查到，但旧的 SIGTERM 收尾会把新任务一起带走。
#
# 所以本脚本的规矩：**任何"成功"都必须以"等几秒后它还活着"为准**。
#
# 用法：
#   launchd-job.sh check  <plist>
#   launchd-job.sh install <plist>
#   launchd-job.sh restart <plist>
#   launchd-job.sh status  <plist>
#   launchd-job.sh uninstall <plist>
#
# 退出码：0 = 验证通过；1 = 不通过（原因打在 stderr，可直接贴进排查记录）。
#
# ⚠️ 兼容 bash 3.2（macOS 自带）：不用关联数组、不用 ${var,,}、不用 mapfile。
# ⚠️ 脚本里若出现全角标点，变量一律写 ${VAR} 而不是 $VAR —— bash 3.2 会把
#    全角字符的字节当成变量名的一部分（`${TARGET}」: unbound variable`）。
#
# 环境变量：AGENTS_DIR（默认 ~/Library/LaunchAgents）、WAIT_LIMIT、SETTLE、BOOT_ATTEMPTS

set -uo pipefail

WAIT_LIMIT=${WAIT_LIMIT:-40}   # 等旧进程退出的上限（秒）。覆盖一次 25 秒长轮询 + 收尾
SETTLE=${SETTLE:-3}            # bootstrap 之后等多久再验存活（秒）
BOOT_ATTEMPTS=${BOOT_ATTEMPTS:-3}

die() { echo "❌ $*" >&2; exit 1; }
note() { echo "   $*"; }

domain() { echo "gui/$(id -u)"; }

# 从 plist 取 Label（唯一权威来源 —— 不要求调用方再传一遍，避免两者不一致）
label_of() {
  plutil -extract Label raw "$1" 2>/dev/null || true
}

installed_path() {
  echo "${AGENTS_DIR:-$HOME/Library/LaunchAgents}/$1.plist"
}

is_loaded() {
  launchctl print "$(domain)/$1" >/dev/null 2>&1
}

state_of() {
  launchctl print "$(domain)/$1" 2>/dev/null \
    | sed -n 's/^[[:space:]]*state = //p' | head -1
}

pid_of() {
  launchctl print "$(domain)/$1" 2>/dev/null \
    | sed -n 's/^[[:space:]]*pid = //p' | head -1
}

# KeepAlive 是不是"真"（缺失 / false / 空字典 = 不是常驻）。
# 注意：KeepAlive 写成 <true/> 或带 SuccessfulExit=false 都算常驻意图，
# 但只有字典写法才真的可靠 —— 见 SKILL.md 的坑 ②。
is_persistent() {
  local pa
  pa="$(plutil -extract KeepAlive xml1 -o - "$1" 2>/dev/null || true)"
  case "$pa" in
    *"<true/>"*)     return 0 ;;
    *"SuccessfulExit"*) return 0 ;;
    *)               return 1 ;;
  esac
}

# ── ① 严格校验 plist ──────────────────────────────────────────────
# `plutil -lint` **不够**：它用宽松的旧式解析器，会放过真正的 XML 错误。
# 实测踩到：XML 注释里写了 `--drain`（XML 注释不允许连续两个连字符），
# plutil 报 OK，而 launchd 拒绝加载 —— 表现就是"安装显示成功、任务不在跑"。
validate_plist() {
  local plist="$1" bad=0
  [ -f "$plist" ] || die "plist 不存在：$plist"

  if ! plutil -lint "$plist" >/dev/null 2>&1; then
    echo "❌ plutil -lint 不通过：$plist" >&2
    bad=1
  fi

  # 严格 XML 解析（plistlib 是严格解析器，和 launchd 的判断一致）
  if ! /usr/bin/python3 -c '
import plistlib, sys
try:
    plistlib.loads(open(sys.argv[1], "rb").read())
except Exception as e:
    print(str(e)[:120], file=sys.stderr)
    sys.exit(1)
' "$plist"; then
    echo "❌ 严格 XML 解析失败（launchd 会拒绝加载）：$plist" >&2
    bad=1
  fi

  # 注释里的 `--`：比解析失败更早暴露问题，且报错信息更直白
  if /usr/bin/python3 -c '
import re, sys
txt = open(sys.argv[1], encoding="utf-8", errors="replace").read()
sys.exit(1 if any("--" in m.group(1) for m in re.finditer(r"<!--(.*?)-->", txt, re.S)) else 0)
' "$plist"; then
    :
  else
    echo "❌ XML 注释里出现了连续两个连字符（--）—— XML 不允许，launchd 拒绝加载" >&2
    bad=1
  fi

  [ "$bad" -eq 0 ] || return 1
  echo "✅ plist 校验通过（lint + 严格 XML + 注释无 --）"
}

[ $# -ge 1 ] || die "用法：$0 {check|install|restart|status|uninstall} <plist>"
CMD="$1"
# ⚠️ 顺序要紧：先报"文件不在"，再谈"Label 读不到"。
# 反过来的话，文件根本不存在时报的是"读不到 Label"，原因就指错了 ——
# 这跟本 skill 要消灭的假诊断是同一类错误。
PLIST="${2:-}"
case "$CMD" in
  check|install|restart|status|uninstall) ;;
  *) die "未知命令：$CMD" ;;
esac
[ -n "$PLIST" ] || die "缺少 plist 参数"
# restart 允许只给"已安装的那份"：路径不在就退回 LaunchAgents 里那份。
if [ "$CMD" = "restart" ] && [ ! -f "$PLIST" ]; then
  _alt="${AGENTS_DIR:-$HOME/Library/LaunchAgents}/$(basename "${PLIST%.plist}").plist"
  [ -f "$_alt" ] && PLIST="$_alt"
fi
[ -f "$PLIST" ] || die "plist 不存在：$PLIST"

LABEL="$(label_of "$PLIST")"
[ -n "$LABEL" ] || die "plist 里读不到 Label（这个键必须有，launchctl 靠它认任务）：$PLIST"

bail() {
  echo "❌ $1" >&2
  # ⚠️ 先分清"没写进去"和"写进去了但没起来" —— 它们的修法完全不同。
  # 把权限/路径问题误报成"竞态、重跑一次"，正是本 skill 要消灭的那类假诊断。
  if [ ! -f "$(installed_path "$LABEL")" ]; then
    echo "   原因：plist 没有被复制到 $(installed_path "$LABEL")" >&2
    echo "   常见：目标目录不存在或不可写、被沙箱/权限拦住（cp 会报 Operation not permitted）" >&2
    echo "   先确认：ls -ld \"${AGENTS_DIR:-$HOME/Library/LaunchAgents}\"" >&2
    return 1
  fi
  echo "   这不是配置错误，多半是 bootout/bootstrap 的竞态 —— 重跑一次通常就好：" >&2
  echo "     $0 restart \"$PLIST\"" >&2
  echo "   仍不行：launchctl print $(domain)/$LABEL" >&2
  echo "   系统日志：log show --last 2m --predicate 'process == \"launchd\"'" >&2
  return 1
}

# ── ② 等旧任务真的从 launchd 域里消失 ──────────────────────────────
# 这是"安装脚本报成功、消息没人接"的真根因，修了两次才看清：
# 进程若在长轮询里，收到 SIGTERM 要等这次轮询返回才退出。bootout 后只睡
# 固定 1 秒就 bootstrap，新任务刚起来、旧进程才退出 —— launchd 把这次退出
# 算在新任务头上，新任务立刻变成 state = SIGTERMed：旧的停了、新的也没了。
wait_job_gone() {
  local waited=0
  while is_loaded "$LABEL"; do
    if [ "$waited" -ge "$WAIT_LIMIT" ]; then
      echo "⚠️  旧任务 ${WAIT_LIMIT} 秒仍未退出，继续加载（可能撞竞态）" >&2
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  [ "$waited" -gt 1 ] && note "等旧任务退出用了 ${waited} 秒"
  return 0
}

# ── ③ 加载 = bootout → 等消失 → bootstrap → 等几秒 → 验存活 ────────
do_load() {
  launchctl bootout "$(domain)/$LABEL" 2>/dev/null || true
  wait_job_gone

  local ok=0 attempt=1
  while [ "$attempt" -le "$BOOT_ATTEMPTS" ]; do
    if launchctl bootstrap "$(domain)" "$PLIST" 2>/dev/null; then ok=1; break; fi
    # bootstrap 的替代写法（老系统 / 已 bootout 的场景）
    if launchctl load "$PLIST" 2>/dev/null; then ok=1; break; fi
    sleep 2
    attempt=$((attempt + 1))
  done

  # ⚠️ 到这里**还不能报成功**。实测：bootstrap 返回 0、print 也查得到，
  # 但几秒后任务就没了 —— 唯一可靠的判据是"等几秒，看它是不是还活着"。
  if [ "$ok" -eq 1 ] && is_loaded "$LABEL"; then
    sleep "$SETTLE"
    verify_loaded || return 1
    return 0
  fi
  echo "❌ $LABEL 仍未加载（已重试 ${BOOT_ATTEMPTS} 次）" >&2
  echo "   查看原因：launchctl print $(domain)/$LABEL" >&2
  echo "   系统日志：log show --last 2m --predicate 'process == \"launchd\"'" >&2
  return 1
}

# ── ④ 存活验证：必须按任务类型分开判 ───────────────────────────────
# 常驻任务必须 running；定时任务平时本来就 not running ——
# 对它断言 running 会得到一条**永远失败的假告警**。
verify_loaded() {
  local st pid
  st="$(state_of "$LABEL")"
  pid="$(pid_of "$LABEL")"

  if is_persistent "$PLIST"; then
    if [ "$st" = "running" ] && [ -n "$pid" ]; then
      echo "✅ $LABEL 已加载并确认在跑（pid ${pid}）"
      return 0
    fi
    echo "❌ $LABEL 是常驻任务，但 state=${st:-（查不到）} pid=${pid:-（无）}" >&2
    echo "   期望 state = running —— 否则就是「装好了但没人接」。" >&2
    return 1
  fi

  if [ -n "$st" ]; then
    echo "✅ $LABEL 已加载（定时任务，state = ${st}）"
    return 0
  fi
  echo "❌ $LABEL 查不到 state（未加载）" >&2
  return 1
}

do_status() {
  echo "=== $LABEL ==="
  echo "    plist：$PLIST"
  if is_loaded "$LABEL"; then
    echo "    常驻：$(is_persistent "$PLIST" && echo 是 || echo 否)"
    launchctl print "$(domain)/$LABEL" 2>/dev/null \
      | grep -E "state =|pid =|last exit code|runs =" | sed 's/^/    /'
  else
    if [ -f "$(installed_path "$LABEL")" ]; then
      echo "    ⚠️  plist 已安装但任务未加载（用 $0 restart \"$PLIST\" 修复）"
    else
      echo "    （未安装）"
    fi
  fi
}

do_uninstall() {
  launchctl bootout "$(domain)/$LABEL" 2>/dev/null || true
  wait_job_gone
  rm -f "$(installed_path "$LABEL")"
  echo "✅ 已卸载 $LABEL"
}

case "$CMD" in
  check)
    validate_plist "$PLIST" || exit 1
    echo "    常驻：$(is_persistent "$PLIST" && echo 是（验证时要求 running） || echo 否（验证时只要求在册）)"
    echo "    当前是否已加载：$(is_loaded "$LABEL" && echo 是 || echo 否)"
    ;;
  install)
    validate_plist "$PLIST" || exit 1
    dest="$(installed_path "$LABEL")"
    mkdir -p "$(dirname "$dest")" || die "建不了目录：$(dirname "$dest")"
    cp "$PLIST" "$dest" || die "复制失败：${PLIST} → ${dest}（目录不可写？被沙箱拦住？）"
    # 复制这一步必须验落盘 —— 不然失败会一路滑到后面变成"任务没加载"，
    # 报出来的原因就全错了（cp 失败时 launchctl 那边什么都不知道）。
    [ -f "$dest" ] || die "复制后 $dest 不存在"
    chmod 644 "$dest"
    note "已复制到 $dest"
    do_load || { bail "加载后存活检查失败"; exit 1; }
    ;;
  restart)
    # 允许只给"已安装的那份"：优先用传进来的路径，它不在就退回 LaunchAgents 里那份。
    [ -f "$PLIST" ] || PLIST="$(installed_path "$LABEL")"
    [ -f "$PLIST" ] || die "找不到 plist：传进来的路径和 $(installed_path "$LABEL") 都不存在（先 install）"
    do_load || { bail "加载后存活检查失败"; exit 1; }
    ;;
  status)    do_status ;;
  uninstall) do_uninstall ;;
esac
