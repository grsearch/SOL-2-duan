# SOL 二段交易监控系统

Solana 链上二段交易机会发现工具。自动扫描新币，通过安全检查后持续监控 X (Twitter) 热度趋势，发现热度暴涨时推送 Discord 通知。

## 架构

```
DexScreener (免费) ──┐
Birdeye    (付费) ──┤── monitor.py ──┬── Dashboard (:8888)
Rugcheck   (免费) ──┤   (asyncio)   ├── Discord Webhook
X/Twitter  (按量) ──┘               └── JSON 持久化
```

## 数据流

1. **DexScreener 扫描** (每5min) → 发现候选币 `Age≥2h, MCAP≥$200K, Liq≥$50K, Vol≥$300K`
2. **安全检查** → Birdeye 安全模块 + Rugcheck LP 锁定检查
3. **进入监控** → Birdeye 每30s 轮询 FDV/LP/Volume/Holders
4. **X 热度** → 每15min 查询, 含 KOL 加权 + CA 精确匹配
5. **Discord 通知** → 热度涨幅 >50% 自动推送
6. **自动退出** → FDV<$50K / Age>24h / 连续2次低热度

## 安装

### 方式一: 一键安装脚本

```bash
git clone https://github.com/YOUR_USER/sol-monitor.git
cd sol-monitor
sudo bash scripts/install.sh
```

安装脚本会自动完成: 创建服务用户、部署到 `/opt/sol-monitor`、创建 Python 虚拟环境、注册 systemd 服务。

### 方式二: 手动安装

```bash
# 1. 克隆
git clone https://github.com/YOUR_USER/sol-monitor.git
cd sol-monitor

# 2. 部署
sudo mkdir -p /opt/sol-monitor/{data,logs}
sudo cp -r monitor.py requirements.txt frontend/ /opt/sol-monitor/
sudo cp .env.example /opt/sol-monitor/.env

# 3. Python 环境
sudo python3 -m venv /opt/sol-monitor/venv
sudo /opt/sol-monitor/venv/bin/pip install -r /opt/sol-monitor/requirements.txt

# 4. systemd
sudo cp systemd/sol-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable sol-monitor
```

## 配置

编辑 `/opt/sol-monitor/.env`:

```bash
sudo nano /opt/sol-monitor/.env
```

**必填项:**
| 变量 | 说明 | 获取方式 |
|------|------|----------|
| `X_BEARER_TOKEN` | X API 认证令牌 | [developer.x.com](https://developer.x.com) |
| `BIRDEYE_API_KEY` | Birdeye 付费 API Key | [birdeye.so](https://birdeye.so) |

**可选项:**
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_PORT` | `8888` | Dashboard 端口 |
| `DISCORD_WEBHOOK_URL` | 已预填 | Discord 通知地址 |
| `HEAT_SPIKE_PERCENT` | `50` | 热度涨幅通知阈值 (%) |
| `EXIT_FDV_THRESHOLD` | `50000` | FDV 退出阈值 ($) |
| `EXIT_AGE_HOURS` | `24` | 最大监控时长 (小时) |

所有配置项见 `.env.example`。

## 使用

```bash
# 启动
sudo systemctl start sol-monitor

# 查看状态
sudo systemctl status sol-monitor

# 实时日志
sudo journalctl -u sol-monitor -f

# 应用日志
tail -f /opt/sol-monitor/logs/monitor.log

# 停止
sudo systemctl stop sol-monitor

# 重启
sudo systemctl restart sol-monitor
```

Dashboard 打开浏览器访问: `http://YOUR_SERVER_IP:8888`

## 升级

```bash
cd sol-monitor
git pull
sudo bash scripts/upgrade.sh
```

升级脚本会自动备份当前版本、更新文件、重启服务。

## 卸载

```bash
sudo bash scripts/uninstall.sh
```

保留数据目录和 `.env`，完全清除需手动删除 `/opt/sol-monitor`。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/coins` | 活跃监控币列表 |
| `GET` | `/api/coins?all=true` | 所有币 (含已退出) |
| `GET` | `/api/coins/{ca}` | 单币详情 + 热度历史 |
| `POST` | `/api/coins` | 手动添加 `{contract_address, symbol}` |
| `DELETE` | `/api/coins/{ca}` | 手动删除 |
| `GET` | `/api/status` | 系统状态 |
| `GET` | `/api/config` | 当前配置 |
| `POST` | `/api/config` | 动态修改配置 |

## X 热度算法

```
Heat = mentions × avg_engagement × (1 + kol_ratio × 2.5) × (1 + ca_ratio × 0.3)
```

- 查询策略: `($TICKER OR "CA前20位") lang:en -is:reply -is:retweet min_faves:3`
- 使用 `since_id` 增量拉取, 避免重复扣费
- KOL 加权: 粉丝>10K 且互动>20 的帖额外权重
- CA 提及: 包含合约地址的帖视为高质量讨论

预估日成本: $5-15 (≤10 个币)

## 目录结构

```
sol-monitor/
├── monitor.py              # 后端主程序
├── requirements.txt
├── .env.example            # 配置模板
├── .gitignore
├── frontend/
│   └── static/
│       └── index.html      # Dashboard (纯HTML, 无需构建)
├── systemd/
│   ├── sol-monitor.service  # systemd 服务定义
│   └── sol-monitor.logrotate
├── scripts/
│   ├── install.sh          # 一键安装
│   ├── upgrade.sh          # 升级脚本
│   └── uninstall.sh        # 卸载脚本
└── data/                   # 运行时数据 (gitignore)
```

## License

MIT
