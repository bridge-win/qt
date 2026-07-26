#!/usr/bin/env bash
# One-command launcher for the QT multi-strategy platform.
#
#   ./start.sh init         interactive setup wizard (venue, budget, risk, alerts)
#   ./start.sh              first run: creates venv + installs + seeds .env,
#                           then starts all strategies + dashboard (foreground)
#   ./start.sh daemon       same, but in the background (PID in data/runtime/qt.pid)
#   ./start.sh stop         stop the background instance
#   ./start.sh status       health of the background instance + per-strategy state
#   ./start.sh fetch        backfill 3y of OHLCV/funding/Fear&Greed into data/parquet/
#   ./start.sh backtest     run the composite-score backtest on local data
#   ./start.sh bt <name>    backtest a gallery strategy (dca|trend|carry), synthetic-ok
#   ./start.sh report       monthly honesty report: each strategy vs plain DCA + P&L
#   ./start.sh test         run the test suite
#
# Everything is idempotent — re-running never breaks an existing setup.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"
PIDFILE="data/runtime/qt.pid"
LOGFILE="data/runtime/qt.log"

bootstrap() {
    if [ ! -x "$PY" ]; then
        echo "[qt] creating virtualenv in $VENV ..."
        python3 -m venv "$VENV"
    fi
    if ! "$PY" -c "import qt" >/dev/null 2>&1; then
        echo "[qt] installing dependencies (first run takes a few minutes) ..."
        "$PY" -m pip install -q --upgrade pip
        "$PY" -m pip install -q -e "packages/btc-backtest"
        "$PY" -m pip install -q -e ".[dev]"
    fi
    if [ ! -f .env ] && [ -f .env.example ]; then
        cp .env.example .env
        echo "[qt] created .env from .env.example — edit it to enable email/Telegram alerts"
    fi
    mkdir -p data/runtime
}

run_foreground() {
    echo "[qt] starting all strategies + dashboard at http://127.0.0.1:8765"
    echo "[qt] per-strategy pages: /strategy/dca /strategy/capitulation /strategy/trend /strategy/carry"
    exec "$PY" scripts/run_all.py "$@"
}

run_daemon() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "[qt] already running (pid $(cat "$PIDFILE")) — use ./start.sh stop first"
        exit 1
    fi
    nohup "$PY" scripts/run_all.py "$@" >"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
    echo "[qt] started in background (pid $(cat "$PIDFILE"))"
    echo "[qt] dashboard: http://127.0.0.1:8765   log: $LOGFILE"
}

stop_daemon() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill "$(cat "$PIDFILE")"
        rm -f "$PIDFILE"
        echo "[qt] stopped"
    else
        rm -f "$PIDFILE"
        echo "[qt] not running"
    fi
}

show_status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "[qt] running (pid $(cat "$PIDFILE"))"
    else
        echo "[qt] background instance not running"
    fi
    if command -v curl >/dev/null 2>&1; then
        curl -sf http://127.0.0.1:8765/api/strategies 2>/dev/null \
            | "$PY" -c "import json,sys; d=json.load(sys.stdin); [print(f\"  {s['name']:<14} {s['status']:<10} cycle={s['cycle']} err={s['last_error'] or '-'}\") for s in d['strategies']]" \
            || echo "  (dashboard not reachable on :8765)"
    fi
}

cmd="${1:-run}"
[ $# -gt 0 ] && shift

case "$cmd" in
    run)      bootstrap; run_foreground "$@" ;;
    daemon)   bootstrap; run_daemon "$@" ;;
    stop)     stop_daemon ;;
    status)   bootstrap; show_status ;;
    fetch)    bootstrap; "$PY" scripts/fetch_history.py --days 1095 "$@" ;;
    backtest) bootstrap; "$VENV/bin/qt" --config config/default.yaml backtest "$@" ;;
    bt)       bootstrap; "$VENV/bin/qt" strategy run "${1:-dca}" "${@:2}" ;;
    report)   bootstrap; "$PY" scripts/monthly_report.py "$@" ;;
    test)     bootstrap; "$PY" -m pytest -q "$@" ;;
    setup)    bootstrap; echo "[qt] setup complete" ;;
    init)     bootstrap; "$PY" scripts/init_wizard.py ;;
    *)
        echo "usage: ./start.sh [init|run|daemon|stop|status|fetch|backtest|bt <name>|report|test|setup]"
        exit 2
        ;;
esac
