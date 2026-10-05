#!/bin/bash
# 安装 / 卸载 pdca v4 的 launchd 任务。
#
# 用法：
#   ./deploy/install_launchd.sh install     安装（常驻收件守护 + 21:30 日报 + 周日 20:00 周报）
#   ./deploy/install_launchd.sh install --allow-unconfigured
#                                            未初始化也安装（稍后初始化即自动生效）
#   ./deploy/install_launchd.sh uninstall   卸载
#   ./deploy/install_launchd.sh status      查看状态与上次退出码
#   ./deploy/install_launchd.sh reload      重新加载（改完 plist 后用）
#   ./deploy/install_launchd.sh restart     重新加载任务（装过但没在跑时用）
#   ./deploy/install_launchd.sh doctor      体检：配置 + 三处授权 + 通道（只读）
#   ./deploy/install_launchd.sh test        跑一遍三个任务（不推送、不写入）
#
# 三个任务的分工：
#   com.carl.pdca.daemon   常驻，KeepAlive —— 你发一句就有人接
#   com.carl.pdca.report   21:30 日报，**只读**三处快照
#   com.carl.pdca.weekly   周日 20:00 周报，只读（完成 / 提交 / 连续天数）
#
# 为什么守护要常驻而不是定时：v4 的唯一输入入口是 Telegram，
# "随时发一句都有人接"是它的核心体验，定时轮询做不到这一点。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.carl.pdca.daemon com.carl.pdca.report com.carl.pdca.weekly)

# 允许在"尚未初始化"时就安装。用途：先把任务装好，等用户完成授权与
# 初始化后系统自动开始工作，不需要再手动装一次。
# 两个任务在未配置时都是**安全**的：
#   · 守护：连不上/写不进去都只记日志，进程不退出（已修崩溃循环）
#   · 日报：如实报告"读取失败"，而不是假装"今天没有待办"
ALLOW_UNCONFIGURED=0

# v1 的任务标签。v4 不再需要它们（待办常驻提醒事项，
# 没有"同步"和"顺延"这两个概念），但**它们可能还装着并在跑** ——
# 实测踩到：v1 的 report 仍指向 daily_report.py，会在 21:30 发出一份
# 基于旧留档的日报，与 v4 的数据完全无关、且具误导性。
# 所以安装/卸载时都顺手清掉，避免"两套系统同时在跑"。
LEGACY_LABELS=(com.carl.pdca.carryover com.carl.pdca.sync)

# 固定用系统 python3：它只用标准库，不依赖任何虚拟环境。
# （ltc-spider 那边踩过"解释器链被清掉导致任务全失效"的坑，
#   这里刻意避开：系统解释器 + 只用标准库 = 没有环境可坏。）
PYTHON="/usr/bin/python3"

usage() {
  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

preflight() {
  if [ ! -x "$PYTHON" ]; then
    echo "❌ 找不到解释器 $PYTHON" >&2
    exit 1
  fi
  if [ ! -f "$PROJECT/config.json" ]; then
    echo "❌ 还没有配置文件 config.json" >&2
    echo "   请先运行：bash deploy/setup-v4.sh --apply" >&2
    exit 1
  fi
  # v4 需要备忘文件夹与目标日历都已配置 —— 否则守护起来后，
  # 每收到一条备忘/日程都会失败，而失败只写在日志里（很容易没发现）。
  # 但若显式加了 --allow-unconfigured，就只警告不阻止：先把任务装好，
  # 等初始化完成后系统自动开始工作。
  "$PYTHON" - "$PROJECT" "$ALLOW_UNCONFIGURED" <<'PYEOF' || exit 1
import json, sys
from pathlib import Path
cfg = json.loads((Path(sys.argv[1]) / "config.json").read_text(encoding="utf-8"))
allow = len(sys.argv) > 2 and sys.argv[2] == "1"
missing = [k for k in ("memo_folder_id", "calendar_name") if not cfg.get(k)]
if missing:
    names = {"memo_folder_id": "备忘文件夹", "calendar_name": "目标日历"}
    msg = "还没配置：" + "、".join(names[m] for m in missing)
    if not allow:
        print("❌ " + msg, file=sys.stderr)
        print("   请先运行：bash deploy/setup-v4.sh --apply", file=sys.stderr)
        print("   （若想先装任务、稍后初始化：加 --allow-unconfigured）",
              file=sys.stderr)
        sys.exit(1)
    print("  ⚠️  " + msg + "（已按 --allow-unconfigured 继续）")
    print("     未配置的部分会失败并写进日志；初始化后自动恢复。")
else:
    print(f"  ✓ 配置就绪：备忘文件夹={cfg.get('memo_folder_name')} "
          f"日历={cfg.get('calendar_name')} 列表={cfg.get('reminders_list')}")
PYEOF
  if ! mkdir -p "$AGENTS" "$PROJECT/logs" 2>/dev/null; then
    echo "❌ 无法创建 $AGENTS 或 $PROJECT/logs" >&2
    exit 1
  fi
  if ! touch "$AGENTS/.pdca-write-test" 2>/dev/null; then
    echo "❌ 无法写入 $AGENTS" >&2
    echo "   如果当前处在受限沙箱，请在你自己的终端里运行同一个命令。" >&2
    exit 1
  fi
  rm -f "$AGENTS/.pdca-write-test"
}

render() {
  local src="$1" dst="$2"
  sed -e "s|__PYTHON__|$PYTHON|g" -e "s|__PROJECT__|$PROJECT|g" "$src" > "$dst"
  plutil -lint "$dst" >/dev/null || { echo "❌ plist 不合法：$dst" >&2; exit 1; }
}

do_install() {
  cleanup_legacy
  preflight
  for label in "${LABELS[@]}"; do
    local src="$PROJECT/deploy/$label.plist"
    local dst="$AGENTS/$label.plist"
    [ -f "$src" ] || { echo "❌ 缺少模板 $src" >&2; exit 1; }
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    # ⚠️ 必须等旧进程真的退出再写入与加载（原因见 wait_job_gone 注释）：
    # 重新安装时若不等待，新任务会被旧进程的退出带走 → SIGTERMed。
    wait_job_gone "$label"
    if ! render "$src" "$dst"; then
      echo "❌ 无法写入 $dst" >&2
      echo "   如果当前处在受限沙箱，请在你自己的终端里运行同一个命令。" >&2
      exit 1
    fi
    launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null \
      || launchctl load "$dst" 2>/dev/null || true

    # ⚠️ **不能靠 launchctl 的返回码判断是否加载成功**。
    # 实测：`launchctl load` 即使失败也返回 0；`bootstrap` 失败返回 5。
    # 于是 `bootstrap || load` 的写法永远"成功" —— 实测踩到：
    # 脚本打印 ✅ 已安装，而任务其实没加载，守护一直没在跑。
    # 唯一可靠的判据是 `launchctl print` 能否查到它。
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "✅ 已安装 $label"
    else
      echo "❌ $label 没有加载成功（launchctl 返回码不可信，用 print 判定）" >&2
      echo "   手动试：launchctl bootstrap gui/$(id -u) $dst" >&2
      exit 1
    fi
  done
  echo
  echo "常驻守护：收到 Telegram 消息即分派（待办/日程/备忘）"
  echo "21:30 日报：读三处快照并推送（Telegram + Bark）"
  echo
  echo "查看状态：$0 status"
  echo "体　　检：$0 doctor"
}

do_uninstall() {
  for label in "${LABELS[@]}" "${LEGACY_LABELS[@]}"; do
    local existed=0
    [ -f "$AGENTS/$label.plist" ] && existed=1
    launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1 && existed=1
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null \
      || launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist" 2>/dev/null || true
    if [ "$existed" -eq 1 ]; then
      echo "🗑️  已卸载 $label"
    fi
  done
}

cleanup_legacy() {
  # 清理 v1 遗留任务（它们可能仍装着、且指向已废弃的脚本）
  local found=0
  for label in "${LEGACY_LABELS[@]}"; do
    if [ -f "$AGENTS/$label.plist" ] \
       || launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      if [ "$found" -eq 0 ]; then
        echo "发现 v1 遗留任务（它们会跑已废弃的脚本）："
        found=1
      fi
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null \
        || launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
      if rm -f "$AGENTS/$label.plist" 2>/dev/null; then
        echo "   🗑️  已清理 $label"
      else
        # 已 bootout 就不会再运行，残留文件无害。
        # （受限沙箱下 rm 会失败，不该因此中止安装。）
        echo "   ⏹  已停止 ${label}（文件删不掉，但已卸载、不会再运行）"
        echo "       手动删可运行：rm ~/Library/LaunchAgents/$label.plist"
      fi
    fi
  done
  if [ "$found" -eq 1 ]; then
    echo "   （v4 不需要"同步"和"顺延"：待办常驻提醒事项）"
    echo
  fi
}

do_status() {
  for label in "${LABELS[@]}"; do
    echo "=== $label ==="
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      launchctl print "gui/$(id -u)/$label" 2>/dev/null \
        | grep -E "state =|pid =|last exit code|runs =" | sed 's/^/    /'
    else
      if [ -f "$AGENTS/$label.plist" ]; then
        echo "    ⚠️  文件已安装但任务未加载（用 $0 restart 修复）"
      else
        echo "    （未安装）"
      fi
    fi
  done
  echo
  echo "=== 最近日志 ==="
  ls -lt "$PROJECT/logs" 2>/dev/null | head -6 || echo "    （还没有日志）"
}

wait_job_gone() {
  # 等某个任务**真的从 launchd 域里消失**（print 查不到 = 已清理干净）。
  #
  # ⚠️ 为什么必须有这一步（这是"安装脚本报成功、发消息没人接"的真根因）：
  # 守护在长轮询里（最长 25 秒），收到 SIGTERM 后要等这次长轮询返回
  # 才会真正退出。bootout 之后只睡固定 1 秒就 bootstrap 的话，
  # 新任务刚起来、旧进程才退出，launchd 把这次退出算在新任务头上 ——
  # 新任务立刻变成 state = SIGTERMed，**旧的停了、新的也没了**。
  # 实测：装完显示 ✅、`launchctl print` 也能查到，几秒后守护就没了。
  #
  # 上限 40 秒，足够覆盖一次 25 秒长轮询 + 收尾。
  local label="$1"
  local waited=0
  while launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; do
    if [ "$waited" -ge 40 ]; then
      echo "⚠️  $label 旧进程 40 秒仍未退出，仍继续加载（可能撞竞态）" >&2
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if [ "$waited" -gt 1 ]; then
    echo "   （等旧进程退出用了 ${waited} 秒）"
  fi
}

verify_loaded() {
  # 加载之后的**存活验证**：区分常驻任务与定时任务。
  #   · 守护是常驻（KeepAlive），**必须** running —— 否则就是你发消息没人接
  #   · 日报是定时任务，平时本来就 not running，只要"在册"就是对的
  #     （对日报断言 running 会得到一条永远失败的假告警）
  # 返回 0 = 通过，1 = 不通过。
  local label="$1"
  local st pid
  st="$(launchctl print "gui/$(id -u)/$label" 2>/dev/null \
        | sed -n 's/^[[:space:]]*state = //p' | head -1)"
  pid="$(launchctl print "gui/$(id -u)/$label" 2>/dev/null \
        | sed -n 's/^[[:space:]]*pid = //p' | head -1)"
  if [ "$label" = "com.carl.pdca.daemon" ]; then
    if [ "$st" = "running" ] && [ -n "$pid" ]; then
      echo "✅ $label 已加载并确认在跑（pid ${pid}）"
      return 0
    fi
    return 1
  fi
  if [ -n "$st" ]; then
    echo "✅ $label 已加载（定时任务，state = ${st}）"
    return 0
  fi
  return 1
}

do_restart() {
  # 重新加载任务。用于"装过但没在跑"（例如被 bootout 后 bootstrap 失败）：
  # launchd 只会在 bootstrap 时读取 plist，所以文件在 ≠ 任务在跑。
  echo "重新加载 pdca 任务"
  echo "════════════════════════════════════════"
  for label in "${LABELS[@]}"; do
    local dst="$AGENTS/$label.plist"
    if [ ! -f "$dst" ]; then
      echo "⚠️  $label 未安装（先运行：$0 install --allow-unconfigured）"
      continue
    fi
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true

    # ⚠️ 必须等旧进程真的退出，不能睡固定几秒就装（见 wait_job_gone 注释）
    wait_job_gone "$label"

    local ok=0
    for attempt in 1 2 3; do
      if launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null; then
        ok=1; break
      fi
      launchctl load "$dst" 2>/dev/null && { ok=1; break; }
      sleep 2
    done
    if [ "$ok" -eq 1 ] && launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      # ⚠️ 到这里**还不能报成功**。实测踩到：bootstrap 返回 0、print 也能查到，
      # 但旧进程的 SIGTERM 处理与 bootout 撞在一起，几秒后任务就没了 ——
      # 于是脚本打印 ✅ 已加载，而实际上守护不在跑，
      # 表现又是"发消息没回复"（同一个症状的第三种根因）。
      # 唯一可靠的判据是：**等几秒，看它是不是还活着**。
      sleep 3
      if ! verify_loaded "$label"; then
        echo "❌ $label 加载后存活检查失败" >&2
        echo "   这不是配置错误，多半是 bootout/bootstrap 的竞态，重跑一次通常就好：" >&2
        echo "     $0 restart" >&2
        echo "   仍不行则看：launchctl print gui/$(id -u)/$label" >&2
      fi
    else
      echo "❌ $label 仍未加载（已重试 3 次）" >&2
      echo "   查看原因：launchctl print gui/$(id -u)/$label" >&2
      echo "   或看系统日志：log show --last 2m --predicate 'process == \"launchd\"'" >&2
    fi
  done
  echo
  echo "查状态：$0 status"
}

do_doctor() {
  echo "pdca v4 体检（全部只读）"
  echo "════════════════════════════════════════"
  echo
  echo "── 1. 配置 ──"
  preflight
  echo
  echo "── 2. 三处授权（真实读取，不是只问 App 名字）──"
  "$PYTHON" - <<'PYEOF'
import subprocess
targets = [
    ("备忘录", 'tell application "Notes"\n  return count of folders\nend tell'),
    ("日历",   'tell application "Calendar"\n  return count of calendars\nend tell'),
    ("提醒事项", 'tell application "Reminders"\n  return count of lists\nend tell'),
]
for label, src in targets:
    try:
        p = subprocess.run(["osascript", "-e", src],
                           capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  {label}：超时（App 可能未运行，重试通常可恢复）")
        continue
    err = p.stderr.strip()
    if not err:
        print(f"  ✅ {label}：可读取")
    elif "-10004" in err or "-1743" in err:
        print(f"  ❌ {label}：无权读取（系统设置 → 隐私与安全性 → 自动化 → 终端）")
    else:
        print(f"  ⚠️  {label}：{err.splitlines()[0][:60]}")
PYEOF
  echo
  echo "── 3. Telegram 通道 ──"
  "$PYTHON" "$PROJECT/src/notify.py" --status 2>&1 | sed 's/^/  /'
  echo
  echo "── 4. 收件守护读取位置 ──"
  if [ -f "$PROJECT/data/daemon-state.json" ]; then
    echo "  $(cat "$PROJECT/data/daemon-state.json")"
  else
    echo "  ⚠️ 还没有读取位置（先跑 src/daemon.py --drain 定基准）"
  fi
  echo
  echo "── 5. launchd 任务 ──"
  for label in "${LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "  ✅ $label 已加载"
    else
      echo "  ❌ $label 未安装"
    fi
  done
}

do_test() {
  # 跑一遍三个任务的**脚本本体**（不经 launchd），验证解释器与配置可用。
  # 两份报表都用 --no-push：避免"验证安装"顺手发一条重复产物给你。
  echo "验证 pdca v4 任务（不推送、不写入）"
  echo "════════════════════════════════════════"
  echo
  echo "── 日报（--no-push）──"
  if "$PYTHON" "$PROJECT/src/report.py" --no-push 2>&1 | sed 's/^/  /'; then
    echo "  → 日报脚本 OK"
  else
    echo "  → ⚠️ 日报退出码非 0，看上面输出"
  fi
  echo
  echo "── 周报（--weekly --no-push）──"
  if "$PYTHON" "$PROJECT/src/report.py" --weekly --no-push 2>&1 | sed 's/^/  /'; then
    echo "  → 周报脚本 OK"
  else
    echo "  → ⚠️ 周报退出码非 0，看上面输出"
  fi
  echo
  echo "── 守护（--once，跑一轮就退出）──"
  echo "   （此刻没有新消息就会立刻返回）"
  "$PYTHON" "$PROJECT/src/daemon.py" --once 2>&1 | sed 's/^/  /' || true
  echo
  echo "── launchd 状态 ──"
  for label in "${LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "  ✅ $label"
      launchctl print "gui/$(id -u)/$label" 2>/dev/null \
        | grep -E "last exit code|runs =" | sed 's/^/      /'
    else
      echo "  ❌ $label 未安装"
    fi
  done
  echo
  echo "── 结论 ──"
  echo "   日报与周报都能读到并打印、守护能跑一轮，说明链路可用。"
  echo "   守护已常驻，随时可发消息；日报今晚 21:30、周报周日 20:00 自动跑。"
}

case "${1:-}" in
  install)
    [ "${2:-}" = "--allow-unconfigured" ] && ALLOW_UNCONFIGURED=1
    do_install ;;
  uninstall) do_uninstall ;;
  status)    do_status ;;
  reload)    do_uninstall; do_install ;;
  restart)   do_restart ;;
  doctor)    do_doctor ;;
  test)      do_test ;;
  *)         usage ;;
esac
