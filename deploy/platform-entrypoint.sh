#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
    echo "Usage: platform-entrypoint {migrate|api|trading-worker} [arguments...]" >&2
    exit 64
fi

role=$1
shift

case "$role" in
    migrate)
        if [ "$#" -ne 0 ]; then
            echo "The migrate role does not accept additional arguments" >&2
            exit 64
        fi
        exec alembic -c /app/alembic.ini upgrade head
        ;;
    api)
        exec python /app/scripts/run_platform_api.py "$@"
        ;;
    trading-worker)
        exec python /app/scripts/run_trading_worker.py "$@"
        ;;
    *)
        echo "Unknown platform role: $role" >&2
        echo "Allowed roles: migrate, api, trading-worker" >&2
        exit 64
        ;;
esac
