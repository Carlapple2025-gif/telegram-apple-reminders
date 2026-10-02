#!/bin/bash
# 安装 / 卸载 pdca 的 launchd 定时任务。
#
# 用法：
#   ./deploy/install_launchd.sh install     安装（21:30 日报 + 07:00 顺延）
#   ./deploy/install_launchd.sh uninstall   卸载
#   ./deploy/install_launchd.sh status      查看状态与上次退出码
#   ./deploy/install_launchd.sh reload      重新加载（改完 plist 后用）
#   ./deploy/install_launchd.sh test        立即触发两个任务，验证能否在 launchd 下跑通
#
# 两个任务的分工（刻意分开）：
#   21:30 report    —— 只读 + 推送，**不写备忘录**
#   07:00 carryover —— 唯一会写备忘录的任务
# 分开的好处：写入路径只有一个入口，出问题时排查面小。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.carl.pdca.report com.carl.pdca.carryover)

# 固定用自带的 python3：它只用标准库，不依赖任何虚拟环境。
# （ldc-spider 那边踩过"解释器链被 git clean 掉导致任务全失效"的坑，
#   这里刻意避开：用系统解释器 + 只用标准库 = 没有环境可坏。）
PYTHON="/usr/bin/python3"

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

preflight() {
  if [ ! -x "$PYTHON" ]; then
    echo "❌ 找不到解释器 $PYTHON" >&2
    exit 1
  fi
  if [ ! -f "$PROJECT/config.json" ]; then
    echo "❌ 还没有初始化。请先运行：bash deploy/init.sh" >&2
    exit 1
  fi
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
  echo "21:30 日报（只读 + 推送）　07:00 顺延（会写备忘录）"
  echo "查看状态：$0 status"
  echo "立即测试：$0 test"
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

do_test() {
  echo "立即触发两个任务（验证它们能否在 launchd 环境下跑通）……"
  echo
  for label in "${LABELS[@]}"; do
    if ! launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "=== $label（未安装，跳过）==="
      continue
    fi
    echo "=== $label ==="
    launchctl kickstart -k "gui/$(id -u)/$label" 2>&1 | sed 's/^/    /' || true
    sleep 4
    launchctl print "gui/$(id -u)/$label" 2>/dev/null \
      | grep -E "last exit code" | sed 's/^/    /'
  done
  echo
  echo "=== 日志内容 ==="
  for f in "$PROJECT"/logs/*.log; do
    [ -f "$f" ] || continue
    echo "--- $(basename "$f") ---"
    tail -20 "$f" | sed 's/^/    /'
  done
  echo
  echo "⚠️  重点看顺延那个任务：如果日志里出现 -10004 或「越权」，"
  echo "   说明 launchd 拿不到备忘录权限（它与终端的授权是分开的）。"
  echo "   真出现的话告诉我，改成由常驻服务代劳。"
}

case "${1:-}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  status)    do_status ;;
  reload)    do_uninstall; do_install ;;
  test)      do_test ;;
  *)         usage ;;
esac
