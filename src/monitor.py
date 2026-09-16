# -*- coding: utf-8 -*-
import json
import sys
import logging
import re
import os
import signal
import time
import requests
try:
    import fcntl  # Linux 文件锁，防止 cron 并发运行；非 Linux 环境降级为不加锁
except ImportError:
    fcntl = None
from logging.handlers import TimedRotatingFileHandler
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest
from aliyunsdkecs.request.v20140526.StartInstanceRequest import StartInstanceRequest
from aliyunsdkecs.request.v20140526.StopInstanceRequest import StopInstanceRequest
from aliyunsdkecs.request.v20140526.DescribeInstancesRequest import DescribeInstancesRequest

# 修正 urllib3 在 Python 3.12 下引发的 SNI 丢失问题
try:
    from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_
    ssl_.HAS_SNI = True
except Exception:
    pass

import socket
# 强制使用 IPv4 避免 IPv6 黑洞
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res
socket.getaddrinfo = _getaddrinfo_ipv4_only

import warnings
warnings.filterwarnings("ignore")

# 配置文件路径
CONFIG_FILE = '/opt/scripts/aliyun_monitor/config.json'
LOG_FILE    = '/opt/scripts/aliyun_monitor/log/monitor/monitor.log'
# 状态缓存文件：记录每个实例上次发送通知的时间戳 / 启动失败次数
STATE_FILE  = '/opt/scripts/aliyun_monitor/monitor_state.json'
# 运行锁文件：避免上一轮巡检未结束时 cron 再次并发启动导致状态互相覆盖
LOCK_FILE   = '/opt/scripts/aliyun_monitor/monitor.lock'

# 通用事件通知冷却时间（秒）：1 小时内不重复发送
NOTIFY_COOLDOWN = 3600
# 流量超标提醒冷却时间（秒）：24 小时只提醒一次
OVERLIMIT_COOLDOWN = 86400
# 等待实例启动：轮询超时 / 间隔（秒）
START_WAIT_TIMEOUT  = 180
START_POLL_INTERVAL = 10
# 单实例巡检硬超时：覆盖正常的启动轮询时间，避免网络请求永久占用全局运行锁
USER_CHECK_TIMEOUT = START_WAIT_TIMEOUT + 120
# 连续启动失败超过此次数后，降低重试频率（每 30 分钟重试一次，而非每 5 分钟）
MAX_START_FAILURES = 3
# 资源不足时的重试冷却时间（秒）：30 分钟重试一次，而不是彻底放弃
RESOURCE_RETRY_COOLDOWN = 1800
# 连续巡检失败达到此次数后，发送"监控失明"告警提醒人工介入
CHECK_FAILURE_ALERT_THRESHOLD = 3
# aliyunsdkcore 将 request timeout 参数解释为秒，不是毫秒
# CDT 请求只对瞬态网络错误做少量重试，确保总耗时远低于单实例 watchdog
CDT_RETRY_ATTEMPTS = 3
CDT_RETRY_DELAY = 1


class MonitorTimeout(BaseException):
    """单实例巡检超时，必须穿透业务层的 broad except，交由外层 watchdog 处理。"""


_SENSITIVE_QUERY_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])['\"]?"
    r"(?:accesskeyid|accesskeysecret|access_key_id|access_key_secret|"
    r"signature|signature_nonce|signaturenonce|signaturemethod|signature_method|"
    r"signatureversion|signature_version|securitytoken|security_token|"
    r"security-token|sessiontoken|session_token|ststoken|sts_token|"
    r"x-acs-security-token|authorization|credential|token)"
    r"['\"]?\s*[=:]\s*['\"]?)"
    r"(?P<value>[^&\s,;\"'<>()[\]{}]+)",
    re.IGNORECASE,
)


def redact_sensitive_values(text):
    """只隐藏签名查询参数值，保留异常中的主机名和原因，便于排障。"""
    return _SENSITIVE_QUERY_VALUE_RE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        str(text),
    )


def safe_error_text(error):
    return redact_sensitive_values(str(error))

# 初始化日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = TimedRotatingFileHandler(LOG_FILE, when='D', interval=1, backupCount=7, encoding='utf-8')
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(handler)

# ---------- 配置加载 ----------

def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error("配置文件 config.json 不存在")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

# ---------- 状态缓存（防抖 / 失败计数） ----------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存状态文件失败: {safe_error_text(e)}")

def can_notify(state, instance_id, event_key, cooldown=None):
    """判断某事件是否已过冷却期，可以再次发送通知"""
    if cooldown is None:
        cooldown = NOTIFY_COOLDOWN
    last_ts = state.get(instance_id, {}).get(event_key, 0)
    return (time.time() - last_ts) >= cooldown

def mark_notified(state, instance_id, event_key):
    state.setdefault(instance_id, {})[event_key] = time.time()

def get_start_failures(state, instance_id):
    return state.get(instance_id, {}).get('start_failures', 0)

def set_start_failures(state, instance_id, count):
    state.setdefault(instance_id, {})['start_failures'] = count

def reset_start_failures(state, instance_id):
    state.setdefault(instance_id, {})['start_failures'] = 0

# ---------- 消息推送服务 ----------

def sanitize_markdown(text):
    """将 legacy Markdown 特殊字符替换为空格，避免实例名/错误信息中的特殊字符导致消息解析失败"""
    text = str(text)
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, ' ')
    return text.strip()

# 企业微信 Webhook 告警功能（兼容 text 类型）
def send_wework_alert(wework_conf, title, message, color_status):
    """发送企业微信告警"""
    webhook_url = wework_conf.get('webhook_url', '').strip()
    if not webhook_url:
        logger.warning("企业微信 Webhook URL 未配置，跳过告警发送")
        return False

    try:
        icon = "✅" if color_status == "green" else "🔔"
        # 纯文本格式：标题 + 换行 + 详细内容
        content = f"{icon} 【{redact_sensitive_values(title)}】\n\n{redact_sensitive_values(message)}"

        data = {
            "msgtype": "text",
            "text": {
                "content": content
            }
        }

        response = requests.post(webhook_url, json=data, timeout=10)
        result = response.json()

        if result.get('errcode') == 0:
            logger.info("企业微信告警发送成功")
            return True
        else:
            logger.error(f"企业微信告警发送失败: {result.get('errmsg', '未知错误')}")
            return False

    except Exception as e:
        logger.error(f"企业微信告警发送异常: {safe_error_text(e)}")
        return False

# Gotify 告警功能
def send_gotify_alert(gotify_conf, title, message, color_status):
    """发送 Gotify 告警"""
    server_url = (gotify_conf.get('url') or '').strip().rstrip('/')
    token = (gotify_conf.get('token') or '').strip()

    if not server_url or not token:
        logger.warning("Gotify 配置不完整，跳过告警发送")
        return False

    url = f"{server_url}/message?token={token}"
    icon = "✅" if color_status == "green" else "🚨"
    clean_text = message.replace('*', '').replace('`', '')

    payload = {
        "title": f"{icon} {title}",
        "message": clean_text,
        "priority": 5 if color_status == "green" else 8,
        "extras": {
            "client::display": {
                "contentType": "text/plain"
            }
        }
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info("Gotify 告警发送成功")
            return True
        else:
            logger.error(f"Gotify 告警发送失败: HTTP {response.status_code}, {response.text}")
            return False
    except Exception as e:
        logger.error(f"Gotify 告警发送异常: {safe_error_text(e)}")
        return False

# Bark 告警功能
def send_bark_alert(bark_conf, title, message, color_status):
    """发送 Bark 告警"""
    raw_url = (bark_conf.get('bark_url') or '').strip().rstrip('/')
    if not raw_url:
        logger.warning("Bark URL 未配置，跳过告警发送")
        return False

    if raw_url.startswith('http://') or raw_url.startswith('https://'):
        parts = raw_url.split('/')
        device_key = parts[-1]
        base_server = "/".join(parts[:-1])
    else:
        device_key = raw_url
        base_server = "https://api.day.app"

    url = f"{base_server}/push"
    icon = "✅" if color_status == "green" else "🚨"
    clean_text = message.replace('*', '').replace('`', '')

    payload = {
        "device_key": device_key,
        "title": f"{icon} {title}",
        "body": clean_text,
        "group": "阿里云监控",
        "icon": "https://img.alicdn.com/tfs/TB1_uJ4uL1YBuNjSszeXXablFXa-144-144.png"
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        res_data = response.json() if response.status_code == 200 else {}
        if response.status_code == 200 and res_data.get('code') == 200:
            logger.info("Bark 告警发送成功")
            return True
        else:
            logger.error(f"Bark 告警发送失败: HTTP {response.status_code}, {response.text}")
            return False
    except Exception as e:
        logger.error(f"Bark 告警发送异常: {safe_error_text(e)}")
        return False

# 统一发送告警（支持同时推送微信、Gotify、Bark）
def dispatch_alert(wework_conf, gotify_conf, bark_conf, title, message, color_status):
    res_wework = send_wework_alert(wework_conf, title, message, color_status)
    res_gotify = send_gotify_alert(gotify_conf, title, message, color_status)
    res_bark   = send_bark_alert(bark_conf, title, message, color_status)
    # 只要任意一个渠道发送成功，就记为发送成功并进入冷却
    return res_wework or res_gotify or res_bark

def billing_region_for_domain(bill_endpoint):
    """根据 BSS 账单域名推导对应 RegionId。
    国内站 business.aliyuncs.com → cn-hangzhou；
    国际站 business.ap-southeast-1.aliyuncs.com → ap-southeast-1。
    region 与域名站点不一致时 BSS 会报 400 "caller site matches the API domain regionId"。"""
    parts = (bill_endpoint or '').split('.')
    if len(parts) >= 4 and parts[0] == 'business':
        return parts[1]
    return 'cn-hangzhou'

def get_balance_line(user):
    """查询账户可用余额，返回告警消息中的"当前余额"行；失败返回空字符串，不影响告警发送"""
    endpoints = [user.get('bill_endpoint', 'business.ap-southeast-1.aliyuncs.com')]
    for candidate in ('business.aliyuncs.com', 'business.ap-southeast-1.aliyuncs.com'):
        if candidate not in endpoints:
            endpoints.append(candidate)
    for endpoint in endpoints:
        try:
            # 使用与账单域名站点匹配的 client，避免国际账号 "caller site" 400 导致余额查询失败
            client = AcsClient(user['ak'], user['sk'], billing_region_for_domain(endpoint))
            req = CommonRequest()
            req.set_domain(endpoint)
            req.set_version('2017-12-14')
            req.set_action_name('QueryAccountBalance')
            req.set_method('POST')
            req.set_protocol_type('https')
            req.set_connect_timeout(5)
            req.set_read_timeout(15)
            data = json.loads(client.do_action_with_exception(req).decode('utf-8'))
            if not data.get('Success'):
                continue
            info = data.get('Data') or {}
            raw_amount = info.get('AvailableAmount')
            if raw_amount is None:
                continue
            amount = float(str(raw_amount).replace(',', ''))
            symbol = {'CNY': '¥', 'USD': '$'}.get(info.get('Currency') or '', user.get('currency', '$'))
            line = f"\n当前余额: {symbol}{amount:.2f}"
            if amount < 0:
                line += " ⚠️"
            return line
        except Exception as e:
            logger.warning(f"查询账户余额失败({endpoint}): {safe_error_text(e)}")
    return ""

# ---------- 查询实例状态 ----------

def get_instance_status(client, instance_id, resgroup=''):
    req_ecs = DescribeInstancesRequest()
    req_ecs.set_protocol_type('https')
    req_ecs.set_connect_timeout(5)
    req_ecs.set_read_timeout(15)
    req_ecs.set_InstanceIds(json.dumps([instance_id]))
    if resgroup:
        req_ecs.set_ResourceGroupId(resgroup)
    resp_ecs = client.do_action_with_exception(req_ecs)
    data_ecs = json.loads(resp_ecs.decode('utf-8'))
    instances = data_ecs.get("Instances", {}).get("Instance", [])
    if not instances:
        return None
    return instances[0].get("Status")


_TRANSIENT_NETWORK_EXCEPTION_NAMES = frozenset({
    'connecttimeout', 'connecttimeouterror', 'readtimeout', 'readtimeouterror',
    'timeout', 'remotedisconnected', 'ssleoferror', 'sslerror', 'protocolerror',
    'maxretryerror', 'newconnectionerror', 'connectionerror',
    'connectionreseterror', 'brokenpipeerror', 'incompleteread',
    'chunkedencodingerror',
})
_TRANSIENT_NETWORK_ERROR_MARKERS = (
    'connecttimeout', 'readtimeout', 'read timed out',
    'remote disconnected', 'remotedisconnected',
    'ssleoferror', 'sslerror', 'max retries exceeded', 'maxretryerror',
    'connection aborted', 'connection reset', 'connection refused',
    'broken pipe', 'incomplete read', 'newconnectionerror',
    'eof occurred in violation',
)


def is_transient_network_error(error):
    """识别 SDK 包装过的网络错误，避免把 API 业务错误纳入重试。"""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))

        if isinstance(current, (ConnectionError, TimeoutError)):
            return True

        error_type = type(current)
        class_name = error_type.__name__.lower()
        if class_name in _TRANSIENT_NETWORK_EXCEPTION_NAMES:
            return True

        error_text = str(current).lower()
        if any(marker in error_text for marker in _TRANSIENT_NETWORK_ERROR_MARKERS):
            return True

        for attribute in ('__cause__', '__context__', 'reason', 'original_error'):
            cause = getattr(current, attribute, None)
            if isinstance(cause, BaseException):
                pending.append(cause)
    return False


def request_cdt_traffic_with_retry(client, request):
    """在一次巡检内重试 CDT 瞬态网络失败，最终错误交给外层统一处理。"""
    for attempt in range(1, CDT_RETRY_ATTEMPTS + 1):
        try:
            return client.do_action_with_exception(request)
        except Exception as error:
            if not is_transient_network_error(error) or attempt >= CDT_RETRY_ATTEMPTS:
                raise
            logger.warning(
                f"CDT流量请求暂时失败（第 {attempt}/{CDT_RETRY_ATTEMPTS} 次）: "
                f"{safe_error_text(error)}，{CDT_RETRY_DELAY}s 后重试"
            )
            time.sleep(CDT_RETRY_DELAY)

# ---------- 核心逻辑 ----------

def check_and_act(user, wework_conf, gotify_conf, bark_conf, state):
    instance_id = user['instance_id']
    name        = user.get('name', instance_id)
    resgroup    = (user.get('resgroup') or '').strip()
    if user.get('paused') or user.get('disabled'):
        logger.info(f"[{name}] 监控已暂停，跳过本轮检查")
        return
    try:
        client = AcsClient(user['ak'], user['sk'], user['region'])

        # 1. 获取流量
        req_traffic = CommonRequest()
        req_traffic.set_domain('cdt.aliyuncs.com')
        req_traffic.set_version('2021-08-13')
        req_traffic.set_action_name('ListCdtInternetTraffic')
        req_traffic.set_method('POST')
        req_traffic.set_protocol_type('https')
        req_traffic.set_connect_timeout(5)
        req_traffic.set_read_timeout(15)
        cdt_client = AcsClient(user['ak'], user['sk'], 'cn-hangzhou')
        resp_traffic = request_cdt_traffic_with_retry(cdt_client, req_traffic)
        data_traffic = json.loads(resp_traffic.decode('utf-8'))
        total_bytes = sum(d.get('Traffic', 0) for d in data_traffic.get('TrafficDetails', []))
        curr_gb = total_bytes / (1024 ** 3)

        # 2. 获取实例当前状态
        status = get_instance_status(client, instance_id, resgroup)
        if status is None:
            logger.error(f"[{name}] 未找到实例: {instance_id}")
            return

        state.setdefault(instance_id, {}).pop('check_failures', None)

        # 3. 决策
        limit = user.get('traffic_limit', 180)

        if curr_gb < limit:
            # ---- 流量安全 ----
            if status == "Stopped":
                failures = get_start_failures(state, instance_id)

                if failures >= MAX_START_FAILURES:
                    last_retry = state.get(instance_id, {}).get('last_retry_ts', 0)
                    elapsed = time.time() - last_retry
                    if elapsed < RESOURCE_RETRY_COOLDOWN:
                        remaining = int(RESOURCE_RETRY_COOLDOWN - elapsed)
                        logger.info(f"[{name}] 已连续 {failures} 次启动失败，"
                                    f"距下次重试还需 {remaining}s，本轮跳过")
                        return
                    logger.info(f"[{name}] 已连续 {failures} 次启动失败，"
                                f"冷却期已过，再次尝试启动...")

                state.setdefault(instance_id, {})['last_retry_ts'] = time.time()
                logger.info(f"[{name}] 流量安全({curr_gb:.2f}GB)，尝试启动实例...")

                try:
                    start_req = StartInstanceRequest()
                    start_req.set_protocol_type('https')
                    start_req.set_connect_timeout(5)
                    start_req.set_read_timeout(15)
                    start_req.set_InstanceId(instance_id)
                    client.do_action_with_exception(start_req)
                    logger.info(f"[{name}] StartInstance API 调用成功，等待实例进入 Running...")
                except Exception as api_err:
                    err_msg = safe_error_text(api_err)
                    new_failures = failures + 1
                    set_start_failures(state, instance_id, new_failures)
                    logger.warning(f"[{name}] StartInstance API 调用失败: {err_msg}，"
                                   f"累计失败 {new_failures} 次")
                    if can_notify(state, instance_id, 'start_failed'):
                        balance_line = get_balance_line(user)
                        msg = (f"机器: {sanitize_markdown(name)}\n当前流量: {curr_gb:.2f}GB{balance_line}\n"
                               f"⚠️ 启动 API 调用失败: {sanitize_markdown(err_msg)}\n"
                               f"累计失败 {new_failures} 次，"
                               f"脚本将每 {RESOURCE_RETRY_COOLDOWN//60} 分钟自动重试。")
                        if dispatch_alert(wework_conf, gotify_conf, bark_conf, "启动失败告警", msg, "red"):
                            mark_notified(state, instance_id, 'start_failed')
                    return

                started = False
                waited  = 0
                while waited < START_WAIT_TIMEOUT:
                    time.sleep(START_POLL_INTERVAL)
                    waited += START_POLL_INTERVAL
                    try:
                        real_status = get_instance_status(client, instance_id, resgroup)
                    except Exception:
                        real_status = "Unknown"
                    logger.info(f"[{name}] 等待启动... 当前状态: {real_status} ({waited}s)")
                    if real_status == "Running":
                        started = True
                        break
                    elif real_status == "Stopped":
                        logger.warning(f"[{name}] 实例已回落到 Stopped 状态，启动被拒绝")
                        break

                if started:
                    reset_start_failures(state, instance_id)
                    state.setdefault(instance_id, {}).pop('no_resource', None)
                    state.setdefault(instance_id, {}).pop('last_retry_ts', None)
                    logger.info(f"[{name}] 实例已恢复运行 ✅")
                    if can_notify(state, instance_id, 'resumed'):
                        balance_line = get_balance_line(user)
                        msg = f"机器: {sanitize_markdown(name)}\n当前流量: {curr_gb:.2f}GB{balance_line}\n动作: 恢复运行 ✅"
                        if dispatch_alert(wework_conf, gotify_conf, bark_conf, "恢复监控", msg, "green"):
                            mark_notified(state, instance_id, 'resumed')
                else:
                    new_failures = failures + 1
                    set_start_failures(state, instance_id, new_failures)
                    logger.warning(f"[{name}] 启动超时或被拒绝，累计失败 {new_failures} 次")
                    if can_notify(state, instance_id, 'start_failed'):
                        balance_line = get_balance_line(user)
                        msg = (f"机器: {sanitize_markdown(name)}\n当前流量: {curr_gb:.2f}GB{balance_line}\n"
                               f"⚠️ 尝试启动但 {START_WAIT_TIMEOUT}s 内未变为 Running 状态，"
                               f"累计失败 {new_failures} 次。\n"
                               f"脚本将每 {RESOURCE_RETRY_COOLDOWN//60} 分钟自动重试，无需手动干预。")
                        if dispatch_alert(wework_conf, gotify_conf, bark_conf, "启动失败告警", msg, "red"):
                            mark_notified(state, instance_id, 'start_failed')

            elif status == "Running":
                reset_start_failures(state, instance_id)
                logger.info(f"[{name}] 流量安全({curr_gb:.2f}GB)，实例运行中")
            else:
                logger.info(f"[{name}] 实例处于中间态: {status}，不干预")

        else:
            # ---- 流量超标 ----
            if status == "Running":
                logger.info(f"[{name}] 流量超标({curr_gb:.2f}GB >= {limit}GB)，正在停止...")
                stop_req = StopInstanceRequest()
                stop_req.set_protocol_type('https')
                stop_req.set_connect_timeout(5)
                stop_req.set_read_timeout(15)
                stop_req.set_InstanceId(instance_id)
                client.do_action_with_exception(stop_req)
                if can_notify(state, instance_id, 'overlimit', OVERLIMIT_COOLDOWN):
                    balance_line = get_balance_line(user)
                    msg = f"机器: {sanitize_markdown(name)}\n当前流量: {curr_gb:.2f}GB{balance_line}\n动作: 已触发止损关机 🛑"
                    if dispatch_alert(wework_conf, gotify_conf, bark_conf, "流量预警", msg, "red"):
                        mark_notified(state, instance_id, 'overlimit')
            else:
                logger.info(f"[{name}] 已停止止损 - {curr_gb:.2f}GB")
                if can_notify(state, instance_id, 'overlimit', OVERLIMIT_COOLDOWN):
                    balance_line = get_balance_line(user)
                    msg = f"机器: {sanitize_markdown(name)}\n当前流量: {curr_gb:.2f}GB{balance_line}\n状态: 流量超标，已保持关机 🛑"
                    if dispatch_alert(wework_conf, gotify_conf, bark_conf, "流量超标提醒", msg, "red"):
                        mark_notified(state, instance_id, 'overlimit')

    except Exception as e:
        err_msg = safe_error_text(e)
        logger.error(f"[{name}] 检查出错: {err_msg}")
        info = state.setdefault(instance_id, {})
        info['check_failures'] = info.get('check_failures', 0) + 1
        if info['check_failures'] >= CHECK_FAILURE_ALERT_THRESHOLD and can_notify(state, instance_id, 'check_failed'):
            msg = (f"机器: {sanitize_markdown(name)}\n"
                   f"⚠️ 已连续 {info['check_failures']} 次巡检失败，最近错误: {sanitize_markdown(err_msg)}\n"
                   f"期间流量监控与自动止损不可用，请人工确认实例状态。")
            if dispatch_alert(wework_conf, gotify_conf, bark_conf, "监控异常告警", msg, "red"):
                mark_notified(state, instance_id, 'check_failed')


def _raise_monitor_timeout(_signum, _frame):
    raise MonitorTimeout()


def check_user_with_timeout(user, wework_conf, gotify_conf, bark_conf, state):
    """执行单实例巡检并设置硬超时，避免某个 SDK 网络请求永久持有全局锁。"""
    if not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL", "setitimer", "getitimer")):
        check_and_act(user, wework_conf, gotify_conf, bark_conf, state)
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _raise_monitor_timeout)
    signal.setitimer(signal.ITIMER_REAL, USER_CHECK_TIMEOUT)
    try:
        check_and_act(user, wework_conf, gotify_conf, bark_conf, state)
    except MonitorTimeout:
        instance_id = user.get('instance_id', '<unknown>')
        name = user.get('name', instance_id)
        info = state.setdefault(instance_id, {})
        info['check_failures'] = info.get('check_failures', 0) + 1
        logger.error(f"[{name}] 单实例巡检超过 {USER_CHECK_TIMEOUT}s，已跳过本实例，避免监控锁长期占用")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)

def acquire_run_lock():
    if fcntl is None:
        return True
    try:
        lock_fp = open(LOCK_FILE, 'w')
    except OSError as e:
        logger.warning(f"无法创建锁文件: {safe_error_text(e)}，本轮不加锁继续执行")
        return True
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        lock_fp.close()
        return None
    return lock_fp

def main():
    lock = acquire_run_lock()
    if lock is None:
        logger.info("上一轮监控仍在运行，本轮跳过")
        return
    try:
        config = load_config()
        state  = load_state()
        wework_conf = config.get('wework', {})
        gotify_conf = config.get('gotify', {})
        bark_conf   = config.get('bark', {})
        for user in config.get('users', []):
            check_user_with_timeout(user, wework_conf, gotify_conf, bark_conf, state)
        save_state(state)
    finally:
        if hasattr(lock, 'close'):
            lock.close()

if __name__ == "__main__":
    main()