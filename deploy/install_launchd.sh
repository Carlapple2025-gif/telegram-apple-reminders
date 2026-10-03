#!/bin/bash
# 安装 / 卸载 pdca 的 launchd 定时任务。
#
# 用法：
#   ./deploy/install_launchd.sh install     安装（常驻收件守护 + 21:30 日报）
#   ./deploy/install_launchd.sh uninstall   卸载
#   ./deploy/install_launchd.sh status      查看状态与上次退出码
#   ./deploy/install_launchd.sh reload      重新加载（改完 plist 后用）
#   ./deploy/install_launchd.sh test        验证链路（**不写入备忘录**）
#   ./deploy/install_launchd.sh test --write  触发真实的写入任务（会改备忘录）
#
# 两个任务的分工（刻意分开）：
#   09:00 sync      —— 把当天页的待办同步到提醒事项（关键一环：没它就没地方打钩）
#   21:30 report    —— 只读 + 推送，**不写备忘录**
#   07:00 carryover —— 唯一会写备忘录的任务
# 分开的好处：写入路径只有一个入口，出问题时排查面小。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.carl.pdca.daemon com.carl.pdca.report)

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
  echo "09:00 同步待办 → 提醒事项"
  echo "21:30 日报（只读 + 推送）"
  echo "07:00 顺延（会写备忘录）"
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
  # 默认**不产生副作用**：顺延任务平时带 --apply（会写备忘录），
  # 但"验证安装"不应该改用户的数据。所以这里手动以干跑方式跑一遍，
  # 只有显式加 --write 才通过 kickstart 触发真实的写入任务。
  #
  # 这个改动来自一次真实事故：早先的 test 无差别 kickstart 了两个任务，
  # 顺延任务立即新建了「次日页」，而当天还没过完 —— 用户第二天会看到
  # 一份基于不完整状态的顺延清单。
  local want_write="no"
  [ "${1:-}" = "--write" ] && want_write="yes"

  echo "验证 pdca 定时任务"
  echo "════════════════════════════════════════"
  echo

  echo "── 1. 直接跑一遍脚本（验证解释器与配置）──"
  echo
  echo "   [日报] 只读 + 推送："
  if "$PYTHON" "$PROJECT/src/daily_report.py" --refresh 2>&1 | sed 's/^/     /'; then
    echo "     → 日报 OK"
  else
    echo "     → ⚠️ 日报退出码非 0，看上面输出"
  fi
  echo
  echo "   [顺延] 干跑（不写入）："
  "$PYTHON" "$PROJECT/src/carry_over.py" --offline 2>&1 | sed 's/^/     /'
  echo "     → 干跑 OK（上面是"计划写入"的内容，未实际写入）"
  echo

  echo "── 2. launchd 任务状态 ──"
  for label in "${LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
      echo "   ✅ $label 已加载"
      launchctl print "gui/$(id -u)/$label" 2>/dev/null \
        | grep -E "last exit code|runs =" | sed 's/^/        /'
    else
      echo "   ❌ $label 未安装"
    fi
  done

  if [ "$want_write" = "yes" ]; then
    echo
    echo "── 3. 真实触发（--write，会写入备忘录）──"
    for label in "${LABELS[@]}"; do
      echo "   kickstart $label"
      launchctl kickstart -k "gui/$(id -u)/$label" 2>&1 | sed 's/^/     /' || true
    done
    sleep 4
    echo
    echo "   任务日志："
    for f in "$PROJECT"/logs/*.log; do
      [ -f "$f" ] || continue
      echo "   --- $(basename "$f") ---"
      tail -12 "$f" | sed 's/^/     /'
    done
  else
    echo
    echo "── 3. 跳过真实触发 ──"
    echo "   顺延任务平时带 --apply（会写备忘录）。为避免在"
    echo "   在「当天还没过完」时提前生成次日页，这里不触发它。"
    echo "   确实要验证写入链路时：$0 test --write"
  fi

  echo
  echo "── 结论 ──"
  echo "   若上面「日报」能读到备忘录并推送、且 launchd 两个任务都已加载，"
  echo "   说明链路可用。真正的首次自动运行：今晚 21:30 日报 / 明早 07:00 顺延。"
}

case "${1:-}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  status)    do_status ;;
  reload)    do_uninstall; do_install ;;
  test)      do_test "${2:-}" ;;
  *)         usage ;;
esac
