#!/bin/bash
# 配置提醒事项列表：确定「待办同步到哪个列表」并写进 config.json。
#
# 为什么要单独一步：
#   待办会**写进**这个列表。写错列表 = 待办混进你的真实数据，
#   而且会持续累积（每天同步一次）。所以这一步必须显式确认，不能静默用默认值。
#
# 用法：
#   bash deploy/setup_reminders.sh              # 用默认名 PDCA
#   bash deploy/setup_reminders.sh --name 我的待办
#   bash deploy/setup_reminders.sh --list       # 只列出所有列表，不改配置

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
CONFIG="${ROOT}/config.json"
LIST_NAME="PDCA"
LIST_ONLY=no

while [ $# -gt 0 ]; do
  case "$1" in
    --name) LIST_NAME="${2:-}"; shift 2 ;;
    --list) LIST_ONLY=yes; shift ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

if [ ! -f "$CONFIG" ]; then
  echo "❌ 缺少 config.json，请先运行：bash deploy/init.sh" >&2
  exit 1
fi

run() { osascript -e "$1" 2>&1; }

echo "提醒事项列表配置"
echo "════════════════════════════════════════"

ACCESS=$(run 'tell application "Reminders" to get name of every list')
if echo "$ACCESS" | grep -q "execution error"; then
  echo "❌ 无法访问提醒事项：${ACCESS}"
  echo "   请到 系统设置 → 隐私与安全性 → 自动化 → 终端 → 勾选「提醒事项」"
  exit 2
fi
echo "✅ 提醒事项可访问"
echo
echo "现有列表："
echo "${ACCESS}" | tr ',' '\n' | sed 's/^ */  · /'

if [ "${LIST_ONLY}" = "yes" ]; then
  exit 0
fi

echo
if echo "${ACCESS}" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -qx "${LIST_NAME}"; then
  echo "目标列表「${LIST_NAME}」已存在，直接使用。"
else
  echo "目标列表「${LIST_NAME}」不存在，将新建它。"
  RESULT=$(run "tell application \"Reminders\"
  make new list with properties {name:\"${LIST_NAME}\"}
  return \"ok\"
end tell")
  echo "  创建结果：${RESULT}"
  # 读回验证
  AFTER=$(run 'tell application "Reminders" to get name of every list')
  if echo "${AFTER}" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -qx "${LIST_NAME}"; then
    echo "  ✅ 已创建并确认"
  else
    echo "  ❌ 创建后读回找不到，请手动在提醒事项里建一个名为「${LIST_NAME}」的列表" >&2
    exit 1
  fi
fi

/usr/bin/python3 - "${CONFIG}" "${LIST_NAME}" <<'PY'
import json, sys, datetime
path, name = sys.argv[1], sys.argv[2]
cfg = json.load(open(path, encoding="utf-8"))
cfg["reminders_list"] = name
cfg["reminders_updated_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"   已写入配置：{path}")
PY

echo
echo "── 配置内容 ──"
cat "${CONFIG}"
echo
echo "✅ 完成。待办将同步到提醒事项的「${LIST_NAME}」列表。"
