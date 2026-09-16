# 阿里云 CDT 流量监控 & 自动止损 & 日报 (国内/国际双站支持)

![OS](https://img.shields.io/badge/OS-Linux-blue?logo=linux)
![Python](https://img.shields.io/badge/Python-3.x-yellow?logo=python)
![Alibaba Cloud](https://img.shields.io/badge/Alibaba%20Cloud-Domestic%20%26%20International-orange?logo=alibabacloud)

一个基于 **阿里云 CDT（云数据传输）** 的 **公网流量监控 + 自动止损 + 每日财报** 工具。  
流量或账单临近失控时自动 **强制关机止损**，次月流量重置后自动开机恢复，全面适配 **国内版（人民币 ¥ 结算）** 与 **国际版（美元 $ 结算）**，同时支持多账号、多地域混合监控。帮你守住钱包 💰。

> **与上游 fork 版的主要区别：**
> - ❌ **摒弃了 Telegram 通知与控制机器人**（`ecs_bot.py` 已移除）
> - ✅ 告警/日报改走 **企业微信 Webhook / Gotify / Bark** 三个轻量渠道
> - 🔧 **修正了国际站账单查询**：`DescribeInstanceBill` 之前因硬编码国内站域名而报 400，现已改为按账号站点的域名 + region 匹配查询

---

## ✨ 核心特性

- 🌍 **双轨支持**：国内站（¥，`business.aliyuncs.com`）与国际站（$，`business.ap-southeast-1.aliyuncs.com`）均可，且每个账号可单独选择站别。
- 🛡️ **流量熔断**：每 5 分钟检测 CDT 流量，超过阈值立即关机止损。
- 💵 **按实例账单**：日报展示每个实例的当月账单（`DescribeInstanceBill`）与账户可用余额（`QueryAccountBalance`）。
- 🔄 **自动恢复**：次月流量重置后自动开机恢复业务；启动失败自动退避重试。
- 📊 **多账号多地域**：同时监控任意组合（不同账号、不同区域、国内/国际混合）。
- 📩 **多端通知**：企业微信 / Gotify / Bark 三个渠道，任一成功即计为已通知（带冷却防刷）。
- 🔒 **仅读即够**：查流量用 `AliyunCDTReadOnlyAccess` 即可，无需写权限。

---

## 🛠️ 前置准备

### 1️⃣ 告警/日报通知渠道（三选一即可，可留空仅记日志）
- **企业微信机器人 Webhook URL**（群机器人地址，支持 text 类型）
- **Gotify**：服务地址 URL + 应用 Token
- **Bark**：推送地址（`https://api.day.app/设备Key` 或自建 Bark 服务地址 / 设备 Key）

### 2️⃣ 阿里云 RAM 权限设置
**强烈建议不要使用主账号**，创建 RAM 子用户并授予以下策略：

| 权限策略 | 用途 |
|----------|------|
| `AliyunCDTReadOnlyAccess` | 查询 CDT 流量（`ListCdtInternetTraffic`）—— 只读即可 |
| `AliyunBSSReadOnlyAccess` | 查询账单与账户余额（`DescribeInstanceBill` / `QueryBillOverview` / `QueryAccountBalance`） |
| `AliyunECSFullAccess` | 查询实例状态 + **自动开关机**（`DescribeInstances` / `StartInstance` / `StopInstance`）|

> ⚠️ 自动止损依赖 ECS 的**开关机写权限**，因此需要 `AliyunECSFullAccess`；查询流量只需 `AliyunCDTReadOnlyAccess`，`FullAccess` 非必需。

### 3️⃣ 需获取的信息
- **AccessKey ID / Secret**（RAM 用户）
- **ECS 实例 ID**（以 `i-` 开头）
- **实例所在地域**（cn-hongkong / ap-southeast-1 / ap-northeast-1 …）
- 账号站别（国内站选 `1`，国际站选 `2`）
- 关机阈值（默认 180 GB）、流量配额 quota（默认 200 GB）

---

## 🚀 一键安装与配置

使用 **root 用户** 在连通互联网的 Linux 服务器上执行（请用 `bash` 运行，不要用 `| sh`）：

```bash
cd /root
wget -qO install.sh https://raw.githubusercontent.com/8220xsk/aliyun_monitor/refs/heads/main/install.sh
bash install.sh
```

> ⚠️ **不要用 `wget -qO- … | sh` / `| bash`**：
> 1. 脚本是 bash 语法，`sh`（dash）会报 `Syntax error: "(" unexpected`。
> 2. 管道方式会让脚本内容占用 stdin，安装过程中的交互输入会读不到。

安装流程会自动：
- 安装系统依赖 + 创建 Python 虚拟环境并安装依赖库
- 拉取 `src/monitor.py`、`src/report.py`
- 引导录入通知渠道、逐账号配置（站别 / AK / SK / 地域 / 实例 / 阈值）
- 生成竖排格式的 `config.json`
- 写入 Cron：**每 5 分钟**巡检、**每天 9 点**发日报

### 安装目录与文件
```
/opt/scripts/aliyun_monitor/
├── config.json                 # 配置文件（AK/SK/Webhook Token，权限 600）
├── monitor.py                  # 流量监控 & 自动止损
├── report.py                   # 每日财报
├── monitor_state.json          # 通知冷却 / 启动失败计数缓存
├── monitor.lock                # 并发运行锁
├── venv/                       # Python 虚拟环境
└── log/
    ├── monitor/                # monitor.py 日志（按天轮转）+ cron.log
    └── report/                 # report.py 日志（按天轮转）+ cron.log
```

---

## ⚙️ 配置文件格式（`/opt/scripts/aliyun_monitor/config.json`）

```json
{
    "bark": { "bark_url": "" },
    "gotify": { "url": "", "token": "" },
    "wework": { "webhook_url": "" },
    "users": [
        {
            "name": "🇭🇰香港01",
            "ak": "LTAI...",
            "sk": "...",
            "region": "cn-hongkong",
            "instance_id": "i-xxx",
            "traffic_limit": 180,
            "quota": 200,
            "bill_endpoint": "business.ap-southeast-1.aliyuncs.com",
            "currency": "$",
            "paused": false
        }
    ]
}
```

- `bill_endpoint` + `currency` 决定站别：国内站 → `business.aliyuncs.com` / `¥`；国际站 → `business.ap-southeast-1.aliyuncs.com` / `$`。
- 每个账号可独立设置站别，支持国内/国际账号混用。
- 账单查询会**按站别自动匹配 region**（国内→`cn-hangzhou`，国际→对应 region），避免“caller site / regionId 不匹配”的 400 报错。

---

## 🎛️ 管理面板

再次运行 `bash install.sh`，检测到 `config.json` 后进入管理菜单：

```
1) 添加新的监控实例 (Add)
2) 删除已有监控实例 (Delete)
3) 暂停/恢复监控实例 (Pause/Resume)
4) 更新脚本并重置所有配置 (Update & Reset)
5) 退出脚本 (Exit)
```

> 选 **4** 才会重新拉取脚本并覆盖 `config.json`；平时加/删/暂停实例都走 1/2/3，不触碰其它配置。

### 手动测试日报
```bash
/opt/scripts/aliyun_monitor/venv/bin/python /opt/scripts/aliyun_monitor/report.py
```

### 查看监控日志
```bash
tail -f /opt/scripts/aliyun_monitor/log/monitor/monitor.log
tail -f /opt/scripts/aliyun_monitor/log/report/report.log
```

---

## 📄 日报内容示例

```
📅 日期: 2026-09-17

👤 *🇭🇰香港01* (2C0.5G)
   🖥️ 状态: 🟢 Running
   🌐 IP: `47.*.*.*`
   📉 流量: 114.51 GB (57.3%)
   💰 账单: *$0.35*
   💳 余额: *$0.00*
   📝 评价: ✅
```

---

## 🗑️ 卸载

```bash
cd /root
wget -qO uninstall.sh https://raw.githubusercontent.com/8220xsk/aliyun_monitor/refs/heads/main/uninstall.sh
bash uninstall.sh
```

卸载脚本只删除 `/opt/scripts/aliyun_monitor` 目录（不会误删 `/opt/scripts` 下其它内容）、清理 Cron 任务。

---

## ⚠️ 免责声明

1. 本项目仅供学习与技术交流使用。
2. 作者不对因脚本异常、API 变更、依赖故障或配置错误导致的流量流失及费用损失直接负责。
3. **强烈建议同时在阿里云费用中心设置「预算告警 / 垫底限额」作为最后防线。**
