#!/usr/bin/env bash
# 从服务器拉取插件 / tools 代码到本仓库（只同步代码，不含数据与备份）
#
# 配置方式（任选其一）：
#   1) 环境变量：SSH_HOST=root@1.2.3.4 SSH_KEY=~/.ssh/id_rsa bash scripts/sync-from-server.sh
#   2) 在仓库根目录放 .env.local（已被 .gitignore 忽略）：
#        SSH_HOST=root@1.2.3.4
#        SSH_KEY=/path/to/key.pem
#        SSH_KNOWN_HOSTS=/path/to/known_hosts
#
# 可选覆盖：REMOTE_PLUGINS / REMOTE_TOOLS / PLUGIN_NAMES
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$REPO_ROOT/.env.local" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env.local"
  set +a
fi

HOST="${SSH_HOST:?请设置 SSH_HOST，例如 SSH_HOST=root@1.2.3.4}"
KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
KNOWN="${SSH_KNOWN_HOSTS:-}"

REMOTE_PLUGINS="${REMOTE_PLUGINS:-/opt/astrbot/data/plugins}"
REMOTE_TOOLS="${REMOTE_TOOLS:-/opt/astrbot}"
PLUGIN_NAMES="${PLUGIN_NAMES:-astrbot_plugin_fish_memory astrbot_plugin_qzone}"

SSH_OPTS=(-i "$KEY" -o ConnectTimeout=20)
[ -n "$KNOWN" ] && SSH_OPTS+=(-o "UserKnownHostsFile=$KNOWN")

# 引号必须保留到远端，否则远端 shell 会展开通配符
REMOTE_EXCLUDES="--exclude='__pycache__' --exclude='*.pyc' --exclude='*.bak-*' \
--exclude='*.ttf' --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' \
--exclude='*.db' --exclude='*.db-*' --exclude='*.log' --exclude='.git'"

mkdir -p "$REPO_ROOT/plugins" "$REPO_ROOT/tools"

echo "==> 同步插件"
ssh "${SSH_OPTS[@]}" "$HOST" \
  "tar czf - -C $REMOTE_PLUGINS $REMOTE_EXCLUDES $PLUGIN_NAMES" \
  | tar xzf - -C "$REPO_ROOT/plugins"

echo "==> 同步 tools"
ssh "${SSH_OPTS[@]}" "$HOST" \
  "tar czf - -C $REMOTE_TOOLS $REMOTE_EXCLUDES tools" \
  | tar xzf - -C "$REPO_ROOT"

echo "==> 插件清单"
ssh "${SSH_OPTS[@]}" "$HOST" \
  "for d in $REMOTE_PLUGINS/*/; do n=\$(basename \$d); [ \"\$n\" = data ] && continue; v=\$(grep -m1 '^version:' \$d/metadata.yaml 2>/dev/null | sed 's/version: *//'); echo \"\$n | \${v:-?}\"; done" \
  > "$REPO_ROOT/plugins/plugin-versions.txt"

echo "==> 完成。git status 查看变化："
git -C "$REPO_ROOT" status --short || true
