#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  SOL 二段监控系统 — 一键安装脚本
#  兼容 Ubuntu 20.04+ / Debian 11+ / Amazon Linux 2023
# ============================================================

INSTALL_DIR="/opt/sol-monitor"
SERVICE_USER="sol-monitor"
SERVICE_NAME="sol-monitor"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ---- Root 检查 ----
[[ $EUID -eq 0 ]] || err "请使用 sudo 运行此脚本: sudo bash $0"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   SOL 二段交易监控系统 — 安装程序               ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ---- 1. 系统依赖 ----
info "安装系统依赖..."
if command -v apt-get &>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip logrotate >/dev/null 2>&1
elif command -v dnf &>/dev/null; then
    dnf install -y -q python3 python3-pip logrotate >/dev/null 2>&1
elif command -v yum &>/dev/null; then
    yum install -y -q python3 python3-pip logrotate >/dev/null 2>&1
else
    warn "未识别的包管理器, 请确保已安装 python3, python3-venv, logrotate"
fi
ok "系统依赖就绪"

# ---- 2. 创建服务用户 ----
if ! id "$SERVICE_USER" &>/dev/null; then
    info "创建服务用户: $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "用户 $SERVICE_USER 已创建"
else
    ok "用户 $SERVICE_USER 已存在"
fi

# ---- 3. 部署文件 ----
info "部署到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"/{data,logs,frontend/static}

# 复制核心文件
cp "$REPO_DIR/monitor.py"           "$INSTALL_DIR/monitor.py"
cp "$REPO_DIR/requirements.txt"     "$INSTALL_DIR/requirements.txt"
cp -r "$REPO_DIR/frontend/static/"  "$INSTALL_DIR/frontend/static/"

# .env — 仅在不存在时创建 (避免覆盖已有配置)
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$REPO_DIR/.env.example" "$INSTALL_DIR/.env"
    warn ".env 已创建, 请编辑填入API密钥: $INSTALL_DIR/.env"
else
    ok ".env 已存在, 跳过覆盖"
fi

ok "文件部署完成"

# ---- 4. Python 虚拟环境 ----
info "创建 Python 虚拟环境..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Python 依赖安装完成"

# ---- 5. 权限 ----
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/.env"
ok "文件权限已设置"

# ---- 6. systemd 服务 ----
info "安装 systemd 服务..."
cp "$REPO_DIR/systemd/sol-monitor.service" /etc/systemd/system/sol-monitor.service
systemctl daemon-reload
systemctl enable sol-monitor.service
ok "systemd 服务已安装并设为开机启动"

# ---- 7. logrotate ----
cp "$REPO_DIR/systemd/sol-monitor.logrotate" /etc/logrotate.d/sol-monitor
ok "logrotate 已配置 (日志自动轮转, 保留14天)"

# ---- 8. 完成 ----
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  ✅ 安装完成!                                    ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
echo "  📁 安装目录:  $INSTALL_DIR"
echo "  📝 配置文件:  $INSTALL_DIR/.env"
echo "  📊 Dashboard: http://YOUR_IP:8888"
echo ""
echo "  ─── 下一步 ───"
echo ""
echo "  1. 编辑配置文件, 填入API密钥:"
echo "     sudo nano $INSTALL_DIR/.env"
echo ""
echo "  2. 启动服务:"
echo "     sudo systemctl start sol-monitor"
echo ""
echo "  3. 查看状态:"
echo "     sudo systemctl status sol-monitor"
echo ""
echo "  4. 查看日志:"
echo "     sudo journalctl -u sol-monitor -f"
echo "     或: tail -f $INSTALL_DIR/logs/monitor.log"
echo ""
echo "  ─── 常用命令 ───"
echo ""
echo "  sudo systemctl stop sol-monitor      # 停止"
echo "  sudo systemctl restart sol-monitor   # 重启"
echo "  sudo systemctl disable sol-monitor   # 取消开机启动"
echo ""
