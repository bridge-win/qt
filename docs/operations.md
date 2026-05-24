# Operations Runbook

This runbook covers local research, backtests, dashboard use, and unattended
paper-mode operation.

## 1. Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Secrets must come from the shell or OS secret store, never from committed files:

```bash
export QT_GLASSNODE_API_KEY=...
export QT_FRED_API_KEY=...
export QT_SANTIMENT_API_KEY=...
```

## 2. Backfill Data

Free sources are enough to run the first backtests:

```bash
python scripts/fetch_history.py --days 1095
qt data sources
```

`qt data sources` shows each data source, how the strategy uses it, whether an
API key is required, local row count, and last-seen timestamp.

## 3. Run Backtests

```bash
qt --config config/default.yaml backtest --output-dir data/backtests
```

Each run writes:

- `data/backtests/<run_id>/summary.json`
- `data/backtests/<run_id>/equity.csv`
- `data/backtests/<run_id>/trades.csv`
- `data/backtests/<run_id>/signals.csv`
- `data/backtests/latest.json`

The dashboard reads `latest.json`, so a new backtest is visible immediately.

## 4. Start The Dashboard

```bash
qt --config config/default.yaml dashboard --port 8765
```

Open `http://127.0.0.1:8765`.

The dashboard displays:

- paper-loop heartbeat and last error
- latest backtest metrics and exported file paths
- data-source coverage, freshness, and usage

## 5. Run Paper Mode With Self-Monitoring

For unattended operation, run the parent watchdog:

```bash
python scripts/run_service.py \
  --config config/default.yaml \
  --interval 3600 \
  --state-path data/runtime/monitor_state.json \
  --stale-after-seconds 7200 \
  --startup-grace-seconds 300 \
  --dashboard-port 8765
```

`run_service.py` starts and supervises:

- `scripts/run_paper.py`, the strategy loop
- `scripts/run_dashboard.py`, unless `--no-dashboard` is passed

The paper loop writes `data/runtime/monitor_state.json` after each cycle. The
parent watchdog restarts the paper loop when:

- the process exits
- the heartbeat becomes stale
- the heartbeat is missing after the startup grace period
- the heartbeat reports `failed`
- the heartbeat reports `stopped`
- the heartbeat timestamp is invalid

The dashboard is also restarted if it exits.

## 6. Health Checks

For a machine-readable health probe:

```bash
qt monitor health \
  --state-path data/runtime/monitor_state.json \
  --stale-after-seconds 7200 \
  --json
```

Exit code is `0` only when the heartbeat is healthy. Use this from cron,
launchd, systemd, or another external monitor.

## 7. Recommended Local launchd Pattern

Use launchd/systemd/pm2 only to keep `run_service.py` itself alive. The Python
service already restarts its child paper/dashboard processes.

Minimum command:

```bash
/Users/kwt/x/qt/.venv/bin/python /Users/kwt/x/qt/scripts/run_service.py \
  --config /Users/kwt/x/qt/config/default.yaml
```

Set the working directory to `/Users/kwt/x/qt` so relative data paths resolve to
`data/`.

## 8. Stop Safely

Send `SIGTERM` or press `Ctrl-C` in the service terminal. The parent process
terminates child processes before exiting.

## 9. Validation Before Capital

Before enabling live trading:

- backtest multiple market windows
- inspect `signals.csv` for factor explanations
- paper trade for at least 90 days
- confirm the health command stays green through restarts
- keep `QT_LIVE_TRADING_ENABLED=false` until exchange-specific live execution
  has been implemented and reviewed
