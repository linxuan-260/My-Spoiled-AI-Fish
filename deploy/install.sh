#!/usr/bin/env bash
# FISH-AI-OS 安装脚本
#
#   bash deploy/install.sh                 # 默认装到 /opt/astrbot
#   APP_DIR=/srv/astrbot bash deploy/install.sh
#
# 做的事：拷插件 → 拷 tools → 写配置模板 → 装 systemd 单元 → enable。
# 不会启动服务，也不会覆盖已有配置。重复执行是安全的（旧目录会改名备份）。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/astrbot}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS="$(date +%m%d_%H%M%S)"

say()  { printf '\033[36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[X] %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0. 前置检查 ────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "需要 root（要写 /etc/systemd/system）"
command -v systemctl >/dev/null || die "需要 systemd"
[ -d "$APP_DIR" ] || die "找不到 $APP_DIR —— 请先装 AstrBot，或用 APP_DIR=... 指定"
[ -x "$APP_DIR/.venv/bin/python" ] || die "缺少 $APP_DIR/.venv/bin/python，请确认 AstrBot 装在 venv 里"

say "安装目录: $APP_DIR"
"$APP_DIR/.venv/bin/python" -V

# ── 1. 插件 ───────────────────────────────────────────
mkdir -p "$APP_DIR/data/plugins" "$APP_DIR/tools"
for p in astrbot_plugin_fish_memory astrbot_plugin_qzone; do
    src="$REPO_ROOT/plugins/$p"
    dst="$APP_DIR/data/plugins/$p"
    [ -d "$src" ] || die "仓库里缺少 plugins/$p"

    if [ -d "$dst" ]; then
        warn "已存在 $dst，改名为 ${p}.bak-install-${TS}"
        mv "$dst" "${dst}.bak-install-${TS}"
    fi
    cp -r "$src" "$dst"
    say "插件已安装: $p"
done

# ── 2. tools ──────────────────────────────────────────
cp -r "$REPO_ROOT/tools/." "$APP_DIR/tools/"
chmod +x "$APP_DIR/tools/fishmem_workers.sh" 2>/dev/null || true
if [ "$APP_DIR" != "/opt/astrbot" ]; then
    sed -i "s#^ROOT=/opt/astrbot#ROOT=$APP_DIR#" "$APP_DIR/tools/fishmem_workers.sh"
    say "已把 fishmem_workers.sh 的 ROOT 改为 $APP_DIR"
fi
say "tools 已安装到 $APP_DIR/tools"

# ── 3. 配置模板（不覆盖已有配置）───────────────────────
CFG="$APP_DIR/data/config/astrbot_plugin_fish_memory_config.json"
mkdir -p "$APP_DIR/data/config"
if [ -f "$CFG" ]; then
    warn "配置已存在，未改动: $CFG"
else
    cp "$REPO_ROOT/config/fish_memory.config.example.json" "$CFG"
    say "已写入配置模板: $CFG"
fi

# ── 4. systemd ────────────────────────────────────────
for unit in astrbot fishmem-panel fishmem-workers; do
    sed "s#__APP_DIR__#$APP_DIR#g" "$REPO_ROOT/deploy/systemd/$unit.service" \
        > "/etc/systemd/system/$unit.service"
    say "已安装 /etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable astrbot fishmem-panel fishmem-workers >/dev/null 2>&1 || true
say "已 enable 三个服务"

# ── 5. 收尾提示 ───────────────────────────────────────
cat <<EOF

──────────────────────────────────────────────
安装完成。还需要手动做两件事：

1. 填你自己的 QQ 号：
     $CFG   ->   master.owner_id

2. 启动（首次）：
     systemctl restart astrbot fishmem-panel fishmem-workers

验证：
     systemctl is-active astrbot fishmem-panel fishmem-workers
     curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:6186/

面板只绑 127.0.0.1，对外请用隧道：
     ssh -N -L 16186:127.0.0.1:6186 user@your-server
──────────────────────────────────────────────
EOF
