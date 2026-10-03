#!/bin/bash
#
# v4 初始化：一条命令做完所有需要授权的事。
#
# 为什么做成脚本：这些步骤有**依赖顺序**（先建文件夹/日历 → 才能收件 →
# 才能装定时任务），散在几条命令里容易漏，而漏了的表现很隐蔽
# （比如没建「备忘」文件夹时，备忘那一路会一直失败，得翻日志才发现）。
#
# 用法：
#   bash deploy/setup-v4.sh              # 干跑：只检查，不改任何东西
#   bash deploy/setup-v4.sh --apply      # 真正执行
#
# 幂等：重复运行安全（已存在的文件夹/日历会复用，不会重复创建）。
# 单独指定名字：
#   bash deploy/setup-v4.sh --apply --memo-folder=备忘 --calendar=PDCA

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
cd "${ROOT}" || exit 1

# 默认用系统 python3 —— 与 launchd 任务保持一致，
# 免得"手动能跑、定时跑不了"这类环境差异。
PY="${PDCA_PYTHON:-/usr/bin/python3}"
APPLY=0
MEMO_FOLDER="${PDCA_MEMO_FOLDER:-备忘}"
CAL_NAME="${PDCA_CALENDAR:-PDCA}"

for arg in "$@"; do
  case "${arg}" in
    --apply) APPLY=1 ;;
    --memo-folder=*) MEMO_FOLDER="${arg#*=}" ;;
    --calendar=*)    CAL_NAME="${arg#*=}" ;;
    -h|--help)
      sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：${arg}" >&2; exit 2 ;;
  esac
done

if [ "${APPLY}" -eq 1 ]; then MODE="执行"; else MODE="干跑（不改任何东西）"; fi

echo "════════════════════════════════════════════════════"
echo "pdca v4 初始化　模式：${MODE}"
echo "  Python：${PY}"
echo "  备忘文件夹：${MEMO_FOLDER}"
echo "  日历：${CAL_NAME}"
echo "════════════════════════════════════════════════════"
echo

# 检查能跑但失败不中止 —— 让你一次看到全貌，而不是修一个跑一次
try() {
  local label="$1"; shift
  echo "── ${label} ──"
  if "$@"; then
    echo
  else
    echo "⚠️ 上一步未成功（继续）"
    echo
    FAILED=$((FAILED + 1))
  fi
}
FAILED=0

# ── ⓪ 先集中检查三处权限（这是最常见的卡点，放最前面）
# 用只读命令探测：失败就明确告诉你去哪儿开哪个开关。
echo "── ⓪ 检查三处授权 ──"
"${PY}" - <<'PYEOF'
import subprocess, sys

# ⚠️ 探测必须**真正读取数据**，不能只问 App 名字。
# 踩到过：`tell application "Notes" to return name` 会成功（那只证明
# App 存在），而任何读取操作都报 -10004 —— 于是检查给出虚假的"✅ 可访问"，
# 让人以为配好了，实际每步都在失败。
# 结论：**"App 能响应"和"能读它的数据"是两个不同的授权级别。**
targets = [
    ("备忘录", 'tell application "Notes"\n  return count of folders\nend tell'),
    ("日历",   'tell application "Calendar"\n  return count of calendars\nend tell'),
    ("提醒事项", 'tell application "Reminders"\n  return count of lists\nend tell'),
]
bad = []
for label, src in targets:
    try:
        p = subprocess.run(["osascript", "-e", src], capture_output=True,
                           text=True, timeout=40)
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  {label}：超时（App 可能未运行，重试通常可恢复）")
        continue
    err = p.stderr.strip()
    if not err:
        print(f"  ✅ {label}：可读取（{p.stdout.strip()} 项）")
    elif "-10004" in err or "-1743" in err:
        print(f"  ❌ {label}：无权读取数据")
        bad.append(label)
    else:
        print(f"  ❌ {label}：{err.splitlines()[0][:60]}")
        bad.append(label)

if bad:
    print()
    print("  需要授权，否则对应功能会一直失败：")
    print("    系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选：")
    for b in bad:
        print(f"      · {b}")
    print()
    print("  若已勾选仍失败，检查「隐私与安全性 → 备忘录」里是否也有终端。")
    print("  提示：授权绑定到**调用进程**；换终端或换用户需重新授权。")
    sys.exit(3)
PYEOF
if [ $? -eq 3 ]; then
  echo
  echo "════════════════════════════════════════════════════"
  echo "先完成上面的授权，再重新运行本脚本。"
  echo "════════════════════════════════════════════════════"
  exit 3
fi
echo

if [ "${APPLY}" -eq 0 ]; then
  try "检查 Python 与依赖" "${PY}" -c "
import sys
sys.path.insert(0, 'src')
for m in ('journal','memo','applecal','reminders','intake','daemon','report',
          'routes','kinds','whens','telegram'):
    __import__(m)
print('  ✅ 全部模块可导入')
"
  try "当前备忘录文件夹（只读）" "${PY}" src/memo.py folders
  try "当前可写日历（只读）" "${PY}" src/applecal.py calendars
  try "拟建备忘文件夹" "${PY}" src/memo.py init --name "${MEMO_FOLDER}"
  try "拟建日历" "${PY}" src/applecal.py new "${CAL_NAME}"
  echo "════════════════════════════════════════════════════"
  echo "以上是干跑，未改动任何东西。确认后加 --apply 执行。"
  echo "════════════════════════════════════════════════════"
  exit 0
fi

# ── 真正执行
try "① 建/复用备忘文件夹" "${PY}" src/memo.py init --name "${MEMO_FOLDER}" --apply
try "② 建/复用日历并设为写入目标" "${PY}" src/applecal.py new "${CAL_NAME}" --apply
try "③ 定下消息读取基准" "${PY}" src/daemon.py --drain

echo "════════════════════════════════════════════════════"
if [ "${FAILED}" -eq 0 ]; then
  echo "✅ 初始化完成。接下来："
else
  echo "⚠️ 有 ${FAILED} 步未成功，请看上面输出。已成功的步骤不会重做。"
fi
echo
echo "  试一条收件（会真的写入）："
echo "    ${PY} src/daemon.py --once"
echo "    然后往 Telegram 发一句，例如「明天交电费」"
echo
echo "  装定时任务（常驻守护 + 21:30 日报）："
echo "    bash deploy/install_launchd.sh install"
echo "════════════════════════════════════════════════════"
