#!/usr/bin/env bash
# 全量敏感信息扫描：工作区 + git 全部历史
#
# 真实值不写死在脚本里（脚本会进公开仓库），从 .deny-list 读取（本地文件，已忽略）。
#
# 用法：  bash scripts/check-secrets.sh
# 退出码：0 = 干净，1 = 命中
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

fail=0

GENERIC='sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|ghp_[A-Za-z0-9]{20,}|BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|(password|passwd|secret|access_?token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{6,}'

echo "== 1/3 工作区：通用密钥特征 =="
hit=$(git grep -nE "$GENERIC" -- . 2>/dev/null | head -5)
if [ -n "$hit" ]; then echo "✗ 命中："; printf '%s\n' "$hit" | sed 's/^/    /'; fail=1
else echo "✓ 无"; fi

echo
echo "== 2/3 工作区：本地黑名单 =="
if [ -f .deny-list ]; then
  while IFS= read -r lit; do
    [ -z "$lit" ] && continue
    case "$lit" in \#*) continue ;; esac
    hit=$(git grep -nF -- "$lit" -- . 2>/dev/null | head -3)
    if [ -n "$hit" ]; then
      echo "✗ 命中【$(printf '%s' "$lit" | cut -c1-3)***】："
      printf '%s\n' "$hit" | sed 's/^/    /'
      fail=1
    fi
  done < .deny-list
  echo "（已检查 $(grep -cvE '^\s*(#|$)' .deny-list) 条）"
else
  echo "· 未找到 .deny-list，跳过"
fi

echo
echo "== 3/3 git 历史（全部提交）=="
hist=$(git log -p --all --no-color 2>/dev/null | grep -nE "$GENERIC" | head -5)
if [ -n "$hist" ]; then echo "✗ 历史中命中通用特征："; printf '%s\n' "$hist" | sed 's/^/    /'; fail=1; fi

if [ -f .deny-list ]; then
  while IFS= read -r lit; do
    [ -z "$lit" ] && continue
    case "$lit" in \#*) continue ;; esac
    hit=$(git log -p --all --no-color 2>/dev/null | grep -nF -- "$lit" | head -2)
    if [ -n "$hit" ]; then
      echo "✗ 历史中命中【$(printf '%s' "$lit" | cut -c1-3)***】："
      printf '%s\n' "$hit" | sed 's/^/    /'
      fail=1
    fi
  done < .deny-list
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "✓✓ 全部通过，可以推送"
  exit 0
else
  echo "✗✗ 发现问题，不要推送"
  exit 1
fi
