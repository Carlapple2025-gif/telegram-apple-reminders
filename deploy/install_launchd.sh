#!/bin/bash
# 安装 / 卸载 pdca v4 的 launchd 任务。
#
# 用法：
#   ./deploy/install_launchd.sh install     安装（常驻收件守护 + 21:30 日报）
#   ./deploy/install_launchd.sh uninstall   卸载
#   ./deploy/install_launchd.sh status      查看状态与上次退出码
#   ./deploy/install_launchd.sh reload      重新加载（改完 plist 后用）
#   ./deploy/install_launchd.sh doctor      体检：配置 + 三处授权 + 通道（只读）
#   ./deploy/install_launchd.sh test        跑一遍两个任务（不推送、不写入）
#
# 两个任务的分工：
#   com.carl.pdca.daemon   常驻，KeepAlive —— 你发一句就有人接
#   com.carl.pdca.report   21:30 日报，**只读**三处快照
#
# 为什么守护要常驻而不是定时：v4 的唯一输入入口是 Telegram，
# "随时发一句都有人接"是它的核心体验，定时轮询做不到这一点。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.carl.pdca.daemon com.carl.pdca.report)

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
  "$PYTHON" - "$PROJECT" <<'PYEOF' || exit 1
import json, sys
from pathlib import Path
cfg = json.loads((Path(sys.argv[1]) / "config.json").read_text(encoding="utf-8"))
missing = [k for k in ("memo_folder_id", "calendar_name") if not cfg.get(k)]
if missing:
    names = {"memo_folder_id": "备忘文件夹", "calendar_name": "目标日历"}
    print("❌ 还没配置：" + "、".join(names[m] for m in missing), file=sys.stderr)
    print("   请先运行：bash deploy/setup-v4.sh --apply", file=sys.stderr)
    sys.exit(1)
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
  preflight
  for label in "${LABELS[@]}"; do
    local src="$PROJECT/deploy/$label.plist"
    local dst="$AGENTS/$label.plist"
    [ -f "$src" ] || { echo "❌ 缺少模板 $src" >&2; exit 1; }
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    render "$src" "$dst"
    launchctl bootstrap "gui/$(id -u)" "$dst" 2>/dev/null || launchctl load "$dst"
    echo "✅ 已安装 $label"
  done
  echo
  echo "常驻守护：收到 Telegram 消息即分派（待办/日程/备忘）"
  echo "21:30 日报：读三处快照并推送（Telegram + Bark）"
  echo
  echo "查看状态：$0 status"
  echo "体　　检：$0 doctor"
}

do_uninstall() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null \
      || launchctl unload "$AGENTS/$label.plist" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "🗑️  已卸载 $label"
  done
}

do_status() {
  for label in "${LABELS[@]}"; do
    echo "=== $label ==="
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      launchctl print "gui/$(id -u)/$label" 2>/dev/null \
        | grep -E "state =|pid =|last exit code|runs =" | sed 's/^/    /'
    else
      echo "    （未安装）"
    fi
  done
  echo
  echo "=== 最近日志 ==="
  ls -lt "$PROJECT/logs" 2>/dev/null | head -6 || echo "    （还没有日志）"
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
  # 跑一遍两个任务的**脚本本体**（不经 launchd），验证解释器与配置可用。
  # 日报用 --no-push：避免"验证安装"顺手发一条重复日报给你。
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
  echo "   日报能读到三处并打印、守护能跑一轮，说明链路可用。"
  echo "   守护已常驻，随时可发消息；日报在今晚 21:30 自动跑。"
}

case "${1:-}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  status)    do_status ;;
  reload)    do_uninstall; do_install ;;
  doctor)    do_doctor ;;
  test)      do_test ;;
  *)         usage ;;
esac
