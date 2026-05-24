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

To upgrade later, run the same one-liner again — it `git pull`s and
`pip install -e .`s in place.

Override defaults via env vars before piping to bash, e.g.:

```bash
curl -fsSL https://raw.githubusercontent.com/bridge-win/qt/main/deploy/aliyun_bootstrap.sh \
  | QT_REPO_REF=main QT_INSTALL_DIR=/opt/qt sudo -E bash
```

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

## Dashboard

The watchdog also starts the local dashboard on `0.0.0.0:8765`. Open port
`8765/tcp` in the Lighthouse firewall (and ufw if active), then visit
`http://<lighthouse-public-ip>:8765`.

For production, front it with nginx + TLS and HTTP basic auth — the
dashboard has no authentication of its own.
