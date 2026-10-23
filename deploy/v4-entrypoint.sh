#!/bin/sh
set -eu
case "${1:-}" in
    migrate|api|research-worker|experiment-worker|data-worker|trading-runtime|watchdog|check|import-legacy|ingest-evidence)
        exec python -m qt.control "$@"
        ;;
    *)
        echo 'Allowed roles: migrate api research-worker experiment-worker data-worker trading-runtime watchdog check import-legacy ingest-evidence' >&2
        exit 64
        ;;
esac
