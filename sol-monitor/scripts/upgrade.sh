#!/usr/bin/env bash
set -euo pipefail

# SOL 二段监控系统 — 升级脚本
# 用法: 在仓库目录内运行 sudo bash scripts/upgrade.sh

[[ $EUID -eq 0 ]] || { echo "请使用 sudo 运行"; exit 1; }

INSTALL_DIR="/opt/sol-monitor"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "🔄 升级 SOL 二段监控系统..."

echo "[1/5] 停止服务..."
systemctl stop sol-monitor 2>/dev/null || true

echo "[2/5] 备份当前版本..."
BACKUP="/opt/sol-monitor-backup-$(date +%Y%m%d-%H%M%S)"
cp -r "$INSTALL_DIR" "$BACKUP"
echo "  备份至: $BACKUP"

echo "[3/5] 更新文件..."
cp "$REPO_DIR/monitor.py"           "$INSTALL_DIR/monitor.py"
cp "$REPO_DIR/requirements.txt"     "$INSTALL_DIR/requirements.txt"
cp -r "$REPO_DIR/frontend/static/"  "$INSTALL_DIR/frontend/static/"
cp "$REPO_DIR/systemd/sol-monitor.service" /etc/systemd/system/sol-monitor.service

echo "[4/5] 更新依赖..."
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "[5/5] 重启服务..."
chown -R sol-monitor:sol-monitor "$INSTALL_DIR"
systemctl daemon-reload
systemctl start sol-monitor

echo ""
echo "✅ 升级完成!  状态: $(systemctl is-active sol-monitor)"
echo "   如有问题可回滚: sudo cp -r $BACKUP/* $INSTALL_DIR/"
