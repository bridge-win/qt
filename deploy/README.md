# Deploying QT to Aliyun Lighthouse (or any Ubuntu ECS)

## One-line install

Pick an **Ubuntu 22.04 LTS** Lighthouse instance (1 vCPU / 2 GB RAM is enough
for the paper loop). SSH in as `root` (or a sudoer) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/bridge-win/qt/main/deploy/aliyun_bootstrap.sh | sudo bash
```

That single command:

1. installs system deps (`python3`, `git`, build tools)
2. creates a hardened `qt` system user under `/opt/qt`
3. clones this repo, builds a venv, installs the project
4. copies `.env.example` → `/opt/qt/.env` (only if absent)
5. installs and starts the `qt.service` systemd unit, which runs
   `scripts/run_service.py` — a watchdog that supervises the paper loop
   and the dashboard, restarting on stale heartbeats.
6. runs `qt-research-worker.service` separately with SQLite WAL job recovery,
   and enables a daily managed-data refresh timer.
7. configures Caddy for authenticated HTTPS at `qt.followkol.live`.

To upgrade later, run the same one-liner again — it `git pull`s and
`pip install -e .`s in place.

## Optional native research workbench worker

The existing dashboard and paper services remain the default deployment. The
unified v3 workbench is opt-in because it requires Python 3.12, Nautilus/TA-Lib,
and strict 256 MiB API / 512 MiB worker ceilings on the small compute node. It
starts a loopback-only `qt-workbench-api` and one worker for bounded research,
import, sync, validation, and optimization jobs from SQLite; it cannot enable
live trading or submit orders.

After verifying capacity and a Python 3.12 executable on the compute node,
deploy it explicitly:

```bash
QT_ENABLE_WORKBENCH=true QT_NATIVE_PYTHON=python3.12 deploy/ssh-deploy.sh
```

Its state is under `/opt/qt/data/workbench`; inspect it with
`systemctl status qt-workbench-api qt-workbench-worker`. Do not enable it for
multi-year minute/tick studies on the current 2-vCPU node.

The API is an authenticated edge origin, not a public listener. Set the
following only in `/opt/qt/.env.workbench` (mode 600, owned by `qt`) after the
Cloudflare Worker has matching secrets:

```env
QT_WORKBENCH_ORIGIN_CLIENT_ID=generated-worker-origin-id
QT_WORKBENCH_ORIGIN_CLIENT_SECRET=generated-worker-origin-secret
# Optional: enables private immutable R2 report publication from the worker.
QT_R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
QT_R2_REPORTS_BUCKET=qt-reports
QT_R2_ACCESS_KEY_ID=...
QT_R2_SECRET_ACCESS_KEY=...
```

Do not put these values in git, browser configuration, or the dashboard `.env`.
Without all four `QT_R2_*` values, completed local reports remain explicitly
unpublished; the API never guesses a download URL.

Override defaults via env vars before piping to bash, e.g.:

```bash
curl -fsSL https://raw.githubusercontent.com/bridge-win/qt/main/deploy/aliyun_bootstrap.sh \
  | QT_REPO_REF=main QT_INSTALL_DIR=/opt/qt sudo -E bash
```

## One-command SSH deploy from this Mac

This repo also supports the same SSH access pattern as the Follow project:
the local deploy script defaults to `SSH_HOST=follow`, syncs this checkout to
`/opt/qt`, and runs the server-side deploy script there.

The local SSH alias should resolve through `~/.ssh/config`:

```sshconfig
Host follow a aliyun ali
  HostName 47.77.239.77
  User root
  IdentityFile ~/.ssh/follow.pem
  IdentitiesOnly yes
```

`ssh follow`、`ssh a`、`ssh aliyun`、`ssh ali` all resolve to the same Aliyun host.

```bash
deploy/ssh-deploy.sh
```

Override the defaults when needed:

```bash
SSH_HOST=follow REMOTE_DIR=/opt/qt WEB_PORT=8765 deploy/ssh-deploy.sh
```

The sync intentionally excludes `.env`, `.git`, `.venv`, caches, logs, and
generated runtime data. Server secrets stay in `/opt/qt/.env`; if that file
does not exist, the remote deploy seeds it from `.env.example`.

The script checks the loopback dashboard and authenticated
`https://qt.followkol.live/api/v2/backtests/health`. Allow inbound TCP 80 and
443 in the Aliyun security group; port 8765 stays bound to loopback.

## Where to put your keys / passwords

Edit `/opt/qt/.env` on the server (mode 600, owned by `qt`). The template
lives at [`.env.example`](../.env.example). The variables that matter:

### Alerts — email (SMTP)

| Variable             | Notes                                                                                   |
| -------------------- | --------------------------------------------------------------------------------------- |
| `QT_SMTP_HOST`       | e.g. `smtp.gmail.com`, `smtp.office365.com`, or `smtpdm.aliyun.com` (Aliyun DirectMail) |
| `QT_SMTP_PORT`       | `465` for SSL (default), `587` for STARTTLS                                             |
| `QT_SMTP_USE_SSL`    | `true` for 465 / `false` for 587                                                        |
| `QT_SMTP_USER`       | your SMTP login (usually the sender email)                                              |
| `QT_SMTP_PASSWORD`   | **app password** — for Gmail/Outlook generate one in your account security page         |
| `QT_SMTP_FROM`       | optional; defaults to `QT_SMTP_USER`                                                    |
| `QT_SMTP_TO`         | comma-separated recipients; defaults to `QT_SMTP_USER`                                  |

> For **Aliyun DirectMail**: create a sender address in the DirectMail
> console, generate an SMTP password, then set
> `QT_SMTP_HOST=smtpdm.aliyun.com`, `QT_SMTP_PORT=465`,
> `QT_SMTP_USER=<your-sender>@<your-domain>`.

### Alerts — Telegram bot (instant push)

1. Message `@BotFather` on Telegram → `/newbot` → save the **bot token**.
2. Send your new bot any message (e.g. `hi`).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy your
   numeric `chat.id`.

Put both into `.env`:

```env
QT_TELEGRAM_BOT_TOKEN=123456:ABC...
QT_TELEGRAM_CHAT_ID=987654321
```

> Telegram works perfectly from Aliyun Mainland ECS only via an outbound
> proxy. If your Lighthouse is in Mainland China, prefer either a HK/SG
> Lighthouse region, or rely on email alerts.

### Exchange / data keys

The same `.env` file holds optional API keys for Binance, OKX, Glassnode,
CryptoQuant, Coinglass, Santiment, LunarCrush, FRED, NewsAPI, CryptoPanic.
Leaving them blank degrades that data source gracefully — the strategy
still runs against whatever data is available.

### Apply changes

```bash
sudo systemctl restart qt
sudo journalctl -u qt -f          # tail logs
```

## Verify alerts are wired

Once `.env` is populated and the service is running, you can trigger a
test alert without waiting for a real signal:

```bash
sudo -u qt /opt/qt/.venv/bin/python -c \
  "from qt.monitoring.alerts import alert; alert('QT alert pipe test', severity='critical', source='manual')"
```

You should receive both an email and a Telegram message within a few
seconds. Failures are logged but never crash the trading loop.

## Dashboard and research lab

The watchdog starts the dashboard on `127.0.0.1:8765`; Caddy is the only public
entry point. Visit:

```text
https://qt.followkol.live/backtest/build
```

The generated Basic Auth credential is stored only on the server:

```bash
sudo cat /etc/qt/research-auth
```

See [`../docs/btc-research-lab-v2.md`](../docs/btc-research-lab-v2.md) for the
beginner workflow, verdict definitions, learning path, and operations.
