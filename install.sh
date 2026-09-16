#!/bin/bash

# 定义颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# GitHub 仓库 raw 下载地址（对应网页仓库: https://github.com/8220xsk/aliyun_monitor/tree/main/src）
# 注意: wget 只能下载 raw 地址，因此这里使用 raw.githubusercontent.com 路径
REPO_URL="https://raw.githubusercontent.com/8220xsk/aliyun_monitor/main/src"
INSTALL_DIR="/opt/scripts/aliyun_monitor"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_FILE="${INSTALL_DIR}/config.json"
LOG_DIR_MONITOR="${INSTALL_DIR}/log/monitor"
LOG_DIR_REPORT="${INSTALL_DIR}/log/report"

# 全局变量，用于在函数间传递生成的 JSON 数据
CURRENT_USER_JSON=""
WEWORK_JSON='{"webhook_url": ""}'
GOTIFY_JSON='{"url": "", "token": ""}'
BARK_JSON='{"bark_url": ""}'

echo -e "${BLUE}=============================================================${NC}"
echo -e "${BLUE}    阿里云 CDT 流量监控 & 日报 一键部署/管理脚本  ${NC}"
echo -e "${BLUE}=============================================================${NC}"

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}请使用 root 权限运行 (sudo -i)${NC}"
  exit 1
fi

# ================= 核心功能函数 =================

# 收集单个用户信息的函数
function get_single_user_json() {
    local AK="" SK="" REGION="" INSTANCE="" NAME="" LIMIT="" QUOTA="" BILL_ENDPOINT="" CURRENCY=""

    echo -e "\n${BLUE}>> 配置阿里云账号/实例信息${NC}"
    read -p "请输入备注名 (例如 HK-Server): " NAME

    echo -e "${CYAN}💡 提示: AccessKey 在 RAM 用户详情页 -> 创建 AccessKey${NC}"
    read -p "AccessKey ID: " AK
    read -p "AccessKey Secret: " SK

    # --- 按实例区分国内外账单体系 ---
    echo -e "\n${CYAN}💡 提示: 请选择该账号所属的阿里云类型 (决定账单查询节点与货币单位)${NC}"
    echo "  1) 国内区 (阿里云中国站，人民币 ￥ 结算)"
    echo "  2) 国际区 (阿里云国际站，美元 $ 结算)"
    read -p "请选择 (1-2, 默认 1): " ACC_TYPE_OPT
    if [ "$ACC_TYPE_OPT" == "2" ]; then
        BILL_ENDPOINT="business.ap-southeast-1.aliyuncs.com"
        CURRENCY="$"
    else
        BILL_ENDPOINT="business.aliyuncs.com"
        CURRENCY="¥"
    fi
    echo -e "${GREEN}已设置为: 账单节点=$BILL_ENDPOINT | 货币=$CURRENCY${NC}\n"

    echo -e "${CYAN}💡 提示: 请选择 ECS 实例所在的区域 (输入数字)${NC}"
    echo "  1) 香港 (cn-hongkong)"
    echo "  2) 新加坡 (ap-southeast-1)"
    echo "  3) 日本-东京 (ap-northeast-1)"
    echo "  4) 美国-硅谷 (us-west-1)"
    echo "  5) 美国-弗吉尼亚 (us-east-1)"
    echo "  6) 德国-法兰克福 (eu-central-1)"
    echo "  7) 英国-伦敦 (eu-west-1)"
    echo "  8) 手动输入其他区域代码"
    read -p "请选择 (1-8): " REGION_OPT

    case $REGION_OPT in
        1) REGION="cn-hongkong" ;;
        2) REGION="ap-southeast-1" ;;
        3) REGION="ap-northeast-1" ;;
        4) REGION="us-west-1" ;;
        5) REGION="us-east-1" ;;
        6) REGION="eu-central-1" ;;
        7) REGION="eu-west-1" ;;
        *) read -p "请输入 Region ID (如 cn-shanghai): " REGION ;;
    esac

    echo -e "${CYAN}💡 提示: 请前往 ECS 控制台 -> 实例列表 -> 实例 ID 列 (以 i- 开头)${NC}"
    read -p "ECS 实例 ID: " INSTANCE

    read -p "关机阈值 (GB, 默认180): " LIMIT
    LIMIT=${LIMIT:-180}

    read -p "流量配额 quota (GB, 默认200): " QUOTA
    QUOTA=${QUOTA:-200}

    # 构建 JSON 字符串: name/ak/sk/region/instance_id/traffic_limit/quota/
    # bill_endpoint/currency/paused
    CURRENT_USER_JSON="{\"name\": \"$NAME\", \"ak\": \"$AK\", \"sk\": \"$SK\", \"region\": \"$REGION\", \"instance_id\": \"$INSTANCE\", \"traffic_limit\": $LIMIT, \"quota\": $QUOTA, \"bill_endpoint\": \"$BILL_ENDPOINT\", \"currency\": \"$CURRENCY\", \"paused\": false}"
}

function ensure_python_env() {
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        echo -e "${GREEN}虚拟环境创建完成。${NC}"
    fi

    echo -e "${YELLOW}>> 安装 Python 依赖库...${NC}"
    "$VENV_DIR/bin/pip" install \
        requests \
        aliyun-python-sdk-core \
        aliyun-python-sdk-ecs \
        aliyun-python-sdk-bssopenapi \
        --upgrade >/dev/null 2>&1
}

function download_runtime_scripts() {
    echo -e "${YELLOW}>> 从 GitHub 下载最新脚本...${NC}"
    wget -q -O "${INSTALL_DIR}/monitor.py" "${REPO_URL}/monitor.py"
    wget -q -O "${INSTALL_DIR}/report.py" "${REPO_URL}/report.py"

    if [ ! -s "${INSTALL_DIR}/monitor.py" ] || [ ! -s "${INSTALL_DIR}/report.py" ]; then
        echo -e "${RED}下载失败！请检查网络或 GitHub 地址是否正确。${NC}"
        exit 1
    fi
}

function collect_notify_channels() {
    local WEWORK_URL="" GOTIFY_URL="" GOTIFY_TOKEN="" BARK_URL=""

    echo -e "\n${BLUE}### 配置告警/日报通知渠道 ###${NC}"
    echo -e "${CYAN}💡 至少配置一个渠道即可收到通知，全部留空则仅记录日志不推送。${NC}"

    read -p "企业微信机器人 Webhook URL (可留空): " WEWORK_URL
    if [ -n "$WEWORK_URL" ]; then
        WEWORK_JSON="{\"webhook_url\": \"$WEWORK_URL\"}"
    fi

    read -p "Gotify 服务地址 URL (可留空): " GOTIFY_URL
    read -p "Gotify 应用 Token (可留空): " GOTIFY_TOKEN
    if [ -n "$GOTIFY_URL" ] && [ -n "$GOTIFY_TOKEN" ]; then
        GOTIFY_JSON="{\"url\": \"$GOTIFY_URL\", \"token\": \"$GOTIFY_TOKEN\"}"
    fi

    read -p "Bark 推送地址 (如 https://api.day.app/设备Key 或直接设备Key, 可留空): " BARK_URL
    if [ -n "$BARK_URL" ]; then
        BARK_JSON="{\"bark_url\": \"$BARK_URL\"}"
    fi
}

# 完整安装流程 (首次运行)
function run_full_install() {
    # 1. 目录准备
    if [ ! -d "$INSTALL_DIR" ]; then
        mkdir -p "$INSTALL_DIR"
        echo -e "${GREEN}创建目录: ${INSTALL_DIR}${NC}"
    fi
    mkdir -p "$LOG_DIR_MONITOR" "$LOG_DIR_REPORT"
    echo -e "${GREEN}创建日志目录: ${LOG_DIR_MONITOR} / ${LOG_DIR_REPORT}${NC}"

    # 2. 安装系统依赖
    echo -e "${YELLOW}>> 安装系统依赖...${NC}"
    if [ -f /etc/debian_version ]; then
        apt-get update -y && apt-get install -y python3 python3-venv python3-pip cron wget
    elif [ -f /etc/redhat-release ]; then
        yum install -y python3 python3-pip cronie wget
        systemctl enable crond && systemctl start crond
    fi

    # 3. 虚拟环境与源码
    ensure_python_env
    download_runtime_scripts

    # 4. 配置通知渠道
    collect_notify_channels

    # 5. 配置阿里云对象
    USERS_JSON=""
    while true; do
        get_single_user_json

        if [ -z "$USERS_JSON" ]; then
            USERS_JSON="$CURRENT_USER_JSON"
        else
            USERS_JSON="$USERS_JSON, $CURRENT_USER_JSON"
        fi

        echo ""
        read -p "是否继续添加下一个账号/实例? (y/n): " CONTIN
        if [[ ! "$CONTIN" =~ ^[Yy]$ ]]; then
            break
        fi
    done

    # 6. 生成配置文件 (用 Python 写，保证竖排缩进 + 正确转义 + UTF-8 保存)
    CONFIG_FILE="$CONFIG_FILE" \
    WEWORK_JSON="$WEWORK_JSON" \
    GOTIFY_JSON="$GOTIFY_JSON" \
    BARK_JSON="$BARK_JSON" \
    USERS_JSON="[$USERS_JSON]" \
    python3 - <<'PY'
import json
import os

config = {
    "bark": json.loads(os.environ["BARK_JSON"]),
    "gotify": json.loads(os.environ["GOTIFY_JSON"]),
    "wework": json.loads(os.environ["WEWORK_JSON"]),
    "users": json.loads(os.environ["USERS_JSON"]),
}
with open(os.environ["CONFIG_FILE"], "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=4)
    f.write("\n")
PY
    # 配置文件包含 AK/SK 与 Webhook Token，收紧为仅 root 可读写
    chmod 600 "$CONFIG_FILE"
    echo -e "${GREEN}配置文件已生成: ${CONFIG_FILE}${NC}"

    # 7. 设置 Crontab
    echo -e "${YELLOW}>> 配置定时任务...${NC}"
    crontab -l > /tmp/cron_bk 2>/dev/null
    grep -v "aliyun_monitor" /tmp/cron_bk > /tmp/cron_clean
    echo "*/5 * * * * ${VENV_DIR}/bin/python ${INSTALL_DIR}/monitor.py >> ${LOG_DIR_MONITOR}/cron.log 2>&1 #aliyun_monitor" >> /tmp/cron_clean
    echo "0 9 * * * ${VENV_DIR}/bin/python ${INSTALL_DIR}/report.py >> ${LOG_DIR_REPORT}/cron.log 2>&1 #aliyun_monitor" >> /tmp/cron_clean
    crontab /tmp/cron_clean
    rm /tmp/cron_bk /tmp/cron_clean

    echo -e "\n${GREEN}🎉 安装与配置完成！${NC}"
    echo -e "您可以使用以下命令手动测试日报发送："
    echo -e "${YELLOW}${VENV_DIR}/bin/python ${INSTALL_DIR}/report.py${NC}"
}

# 管理菜单 (二次运行)
function run_manage_menu() {
    while true; do
        echo -e "\n${GREEN}=====================================${NC}"
        echo -e "${YELLOW}已检测到存在配置文件，请选择管理操作：${NC}"
        echo "1) 添加新的监控实例 (Add)"
        echo "2) 删除已有监控实例 (Delete)"
        echo "3) 暂停/恢复监控实例 (Pause/Resume)"
        echo "4) 更新脚本并重置所有配置 (Update & Reset)"
        echo "5) 退出脚本 (Exit)"
        echo -e "${GREEN}=====================================${NC}"
        read -p "请输入序号 (1-5): " MENU_OPT

        case $MENU_OPT in
            1)
                get_single_user_json
                python3 -c "
import json
with open('$CONFIG_FILE', 'r') as f:
    data = json.load(f)
data['users'].append(json.loads('''$CURRENT_USER_JSON'''))
with open('$CONFIG_FILE', 'w') as f:
    json.dump(data, f, indent=4)
"
                echo -e "${GREEN}✅ 实例添加成功！配置文件已更新。${NC}"
                ;;
            2)
                echo -e "\n${BLUE}当前监控的实例列表：${NC}"
                python3 -c "
import json
with open('$CONFIG_FILE', 'r') as f:
    users = json.load(f).get('users', [])
if not users:
    print('当前没有配置任何监控实例。')
else:
    for i, u in enumerate(users):
        print(f' [{i}] 备注名: {u.get(\"name\")} | 实例ID: {u.get(\"instance_id\")} | 区域: {u.get(\"region\")}')
"
                echo ""
                read -p "请输入要删除的实例序号 (输入 q 取消): " DEL_IDX
                if [[ "$DEL_IDX" == "q" || -z "$DEL_IDX" ]]; then
                    continue
                fi
                python3 -c "
import json, sys
idx = int('$DEL_IDX')
with open('$CONFIG_FILE', 'r') as f:
    data = json.load(f)
try:
    removed = data['users'].pop(idx)
    with open('$CONFIG_FILE', 'w') as f:
        json.dump(data, f, indent=4)
    print(f'\n\033[0;32m✅ 成功删除实例: {removed.get(\"name\")} ({removed.get(\"instance_id\")})\033[0m')
except Exception as e:
    print(f'\n\033[0;31m❌ 删除失败: 无效的序号 {idx}\033[0m')
"
                ;;
            3)
                echo -e "\n${BLUE}当前监控的实例列表：${NC}"
                python3 -c "
import json
with open('$CONFIG_FILE', 'r') as f:
    users = json.load(f).get('users', [])
if not users:
    print('当前没有配置任何监控实例。')
else:
    for i, u in enumerate(users):
        paused = '已暂停' if u.get('paused') or u.get('disabled') else '运行中'
        print(f' [{i}] 备注名: {u.get(\"name\")} | 实例ID: {u.get(\"instance_id\")} | 状态: {paused}')
"
                echo ""
                read -p "请输入要切换暂停/恢复的实例序号 (输入 q 取消): " TOGGLE_IDX
                if [[ "$TOGGLE_IDX" == "q" || -z "$TOGGLE_IDX" ]]; then
                    continue
                fi
                python3 -c "
import json
idx = int('$TOGGLE_IDX')
with open('$CONFIG_FILE', 'r') as f:
    data = json.load(f)
try:
    user = data['users'][idx]
    paused = bool(user.get('paused') or user.get('disabled'))
    user['paused'] = not paused
    user.pop('disabled', None)
    with open('$CONFIG_FILE', 'w') as f:
        json.dump(data, f, indent=4)
    state = '已暂停' if user['paused'] else '已恢复'
    print(f'\n\033[0;32m✅ 成功切换实例: {user.get(\"name\")} ({user.get(\"instance_id\")}) -> {state}\033[0m')
except Exception:
    print(f'\n\033[0;31m❌ 操作失败: 无效的序号 {idx}\033[0m')
"
                ;;
            4)
                echo -e "${RED}⚠️ 此操作将更新代码并覆盖现有的 config.json！${NC}"
                read -p "确认要更新并重置配置吗？(y/n): " CONFIRM_REINSTALL
                if [[ "$CONFIRM_REINSTALL" =~ ^[Yy]$ ]]; then
                    run_full_install
                    exit 0
                fi
                ;;
            5)
                echo -e "${GREEN}退出脚本。${NC}"
                exit 0
                ;;
            *)
                echo -e "${RED}输入无效，请重新选择。${NC}"
                ;;
        esac
    done
}

# ================= 脚本入口 =================

if [ -f "$CONFIG_FILE" ]; then
    # 修正历史版本安装的配置文件权限（包含 AK/SK，避免全局可读）
    chmod 600 "$CONFIG_FILE" 2>/dev/null || true
    # 如果检测到 config.json 已存在，进入管理菜单
    run_manage_menu
else
    # 首次安装
    run_full_install
fi
