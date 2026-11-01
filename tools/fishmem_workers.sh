#!/usr/bin/env bash
# Fish memory workers supervisor (Linux port of tools/fishmem_workers.ps1)
# usage: fishmem_workers.sh {start|stop|status|run|once}
set -uo pipefail

ROOT=/opt/astrbot
PLUGIN="$ROOT/data/plugins/astrbot_plugin_fish_memory"
WORKER="$PLUGIN/worker"
PY="$ROOT/.venv/bin/python"
PDIR="$ROOT/data/plugin_data/astrbot_plugin_fish_memory/logs/pids"
LDIR="$ROOT/data/plugin_data/astrbot_plugin_fish_memory/logs"

mkdir -p "$PDIR" "$LDIR"

NAMES=(ingest life digest)

script_of() {
  case "$1" in
    ingest) printf '%s' "$WORKER/ingest_worker.py" ;;
    life)   printf '%s' "$WORKER/life_daemon.py" ;;
    digest) printf '%s' "$WORKER/daily_digest.py" ;;
  esac
}

alive() {
  local n pidfile pid
  n="$1"
  pidfile="$PDIR/$n.pid"
  [ -f "$pidfile" ] || return 1
  pid=$(cat "$pidfile" 2>/dev/null || true)
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

start_one() {
  local n pid
  n="$1"
  if alive "$n"; then
    echo "  $n already running (pid $(cat "$PDIR/$n.pid"))"
    return 0
  fi
  cd "$PLUGIN" || exit 1
  nohup "$PY" "$(script_of "$n")" >>"$LDIR/$n.out.log" 2>>"$LDIR/$n.err.log" &
  pid=$!
  echo "$pid" >"$PDIR/$n.pid"
  echo "  $n started pid=$pid"
}

stop_one() {
  local n pid
  n="$1"
  if alive "$n"; then
    pid=$(cat "$PDIR/$n.pid")
    kill "$pid" 2>/dev/null || true
    echo "  $n stopped (pid $pid)"
  else
    echo "  $n not running"
  fi
  rm -f "$PDIR/$n.pid"
}

ACTION="${1:-status}"

case "$ACTION" in
  start)
    for n in "${NAMES[@]}"; do start_one "$n"; done
    ;;
  stop)
    for n in "${NAMES[@]}"; do stop_one "$n"; done
    ;;
  status)
    for n in "${NAMES[@]}"; do
      if alive "$n"; then
        echo "  $n RUNNING pid=$(cat "$PDIR/$n.pid")"
      else
        echo "  $n STOPPED"
      fi
    done
    ;;
  once)
    for n in "${NAMES[@]}"; do
      echo "--- $n ---"
      ( cd "$PLUGIN" && "$PY" "$(script_of "$n")" --once )
    done
    ;;
  run)
    cleanup() {
      for n in "${NAMES[@]}"; do stop_one "$n"; done
      exit 0
    }
    trap cleanup TERM INT
    for n in "${NAMES[@]}"; do start_one "$n"; done
    wait
    ;;
  *)
    echo "usage: $0 {start|stop|status|run|once}"
    exit 2
    ;;
esac
