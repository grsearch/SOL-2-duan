#!/usr/bin/env bash
set -euo pipefail

# SOL 二段监控系统 — 卸载脚本

[[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行"; exit 1; }

echo "⚠️  即将卸载 SOL 二段监控系统"
echo "   数据目录 /opt/sol-monitor/data 将被保留"
read -p "确认卸载? (y/N) " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { echo "取消"; exit 0; }

echo "[1/4] 停止服务..."
systemctl stop sol-monitor 2>/dev/null || true
systemctl disable sol-monitor 2>/dev/null || true

echo "[2/4] 移除 systemd 服务..."
rm -f /etc/systemd/system/sol-monitor.service
systemctl daemon-reload

echo "[3/4] 移除 logrotate 配置..."
rm -f /etc/logrotate.d/sol-monitor

echo "[4/4] 清理安装文件 (保留 data/ 目录)..."
cd /opt/sol-monitor 2>/dev/null && {
    # 保留数据
    find . -maxdepth 1 ! -name '.' ! -name 'data' ! -name '.env' -exec rm -rf {} +
    echo "  已保留: /opt/sol-monitor/data/ 和 .env"
} || echo "  /opt/sol-monitor 不存在"

echo ""
echo "✅ 卸载完成"
echo "   如需完全清除, 运行: sudo rm -rf /opt/sol-monitor && sudo userdel sol-monitor"
