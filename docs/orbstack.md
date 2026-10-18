# Local Workbench on OrbStack

This bundle runs the existing research Workbench UI, authenticated API, one
research worker, a public BTC spot monitor, an opportunity-alert worker and an
isolated Codex reviewer on a Mac. It uses Docker
Compose directly on OrbStack, native `linux/arm64` on Apple Silicon and native
`linux/amd64` on Intel. It does not start a trading agent or live orders. No exchange account keys are
needed. Opportunity alerts start in shadow mode; model and Telegram credentials
are optional until their actual authentication/recipient checks pass. See
[BTC alerts](btc-alerts.md) for the full notification setup.

## Start

Open OrbStack and wait for Docker to be ready. Use its current Docker Compose
plugin (2.24+; the bundled commands require `up --wait`). Python 3.10+ is needed
on the Mac only for the private setup helper. From this repository:

```bash
bash deploy/orbstack.sh init
bash deploy/orbstack.sh up
bash deploy/orbstack.sh check
```

`init` is a user-run interactive action: it asks permission, then a local
username and a new local password, with hidden password entry. It hashes the
password using the official Caddy container and saves a fresh internal origin
pair to `.env.orbstack`, owned by the current user with mode 0600. Passwords are
never command-line arguments or environment variables. It never overwrites an
existing file. The initial login setup leaves external credentials blank. The alerts-init
command adds a private internal review secret and supports hidden entry of
explicitly authorized model/Telegram credentials. The shipped example has blank credentials.

Open `http://127.0.0.1:8877` and enter the local login in your browser's prompt.
The first image build downloads pinned Python wheels and builds the React UI,
so allow several minutes. Source/lock changes need the `up` command again;
`compose restart` alone does not apply changed environment settings.

```bash
bash deploy/orbstack.sh status
bash deploy/orbstack.sh down
```

`down` removes this project's containers/network and keeps its data volumes.
Do not add `--volumes` unless you intend to delete this project's datasets and
research history. No commands prune other containers/images/volumes or change
the global Docker context. The wrapper always uses `docker --context orbstack`.

Optional wrapper settings:

- `QT_DOCKER_CONTEXT`: must select orbstack on this Mac
- `QT_ORBSTACK_PROJECT_NAME`: a separate Compose project (default `qt-orbstack`)
- `QT_ORBSTACK_ENV_FILE`: absolute path to a private configuration file
- `PYTHON`: Python executable used by setup/validation

For example, use a distinct project and a distinct port for a second isolated
copy. The wrapper clears inherited application settings so an exported shell
secret cannot silently override the inspected private file.

## Authentication and local access

This is a local, plain-HTTP workflow. Use the loopback URL above. Do not expose
it to a LAN or the internet; remote use needs HTTPS and an appropriately secured
edge. Basic Auth protects both the UI and API. Caddy passes the Workbench's
existing origin ID/secret and an authenticated subject server-side, replacing
client-provided trust headers and stripping the browser Authorization header.
Cross-origin browser requests are rejected. No secret is embedded in web assets.

The API has no published port and requires its origin credentials on every
route when that pair is configured, including documentation/schema routes.
This remains necessary because OrbStack can expose container IPs and
`.orb.local` domains independently of published localhost ports. A localhost
port binding is not an authentication boundary. Caddy's administrative API is
disabled. No host Docker socket is mounted.

Environment injection keeps setup simple and avoids nonroot access problems
with mode-0600 bind-mounted secret files on macOS. Docker administrators can
still inspect a container's environment. Keep `.env.orbstack` private, never
paste full `docker inspect`, `compose config`, `config --environment`, debug
traces, or credentials into chat/logs. `config --quiet` validates without
printing resolved secrets. Both `.gitignore` and `.dockerignore` exclude all
`.env.*` deployment files. Use a private file outside the checkout if preferred.

## Monitor venue and environment

Edit only the private `.env.orbstack` and run `up` again. Supported public-data
pairs are:

| Venue | Environment | Feed |
| --- | --- | --- |
| `binance` | `production` | Public real-market BTC/USDT |
| `binance` | `testnet` | Public sandbox feed |
| `okx` | `production` | Public real-market BTC/USDT |
| `okx` | `demo` | Public demo feed |

State is namespaced as `/state/<venue>-<environment>` in the monitor volume.
The API mounts that volume read-only at `/monitor` and reads only the selected
namespace. Changing a feed cannot silently reuse another source's market state.
The worker's source-identity guards remain in force. These switches never enable
execution or authorize an order. Account-specific fee tier, budget and inventory
remain unknown in a public-data monitor.

The separate `docker-compose.btc-agent.yml` supports existing paper and dedicated
Binance Testnet/OKX Demo flows, documented in `docs/codex-btc-agent.md`. They are
not included in this startup. Never put production trading keys into sandbox
settings, or put exchange credentials in the public monitor file.

## Telegram recipient setup

A bot token authenticates the sender; a chat ID identifies where alerts go.
Rotate any token previously shared in chat through BotFather before using it.
Never put the token into a browser URL, shell argument, source file or this guide.

1. In Telegram, open your bot, press Start, and send it a short message.
2. Run `python3 scripts/telegram_chat_id.py --expected-bot YOUR_BOT_USERNAME`
   in your own terminal, replacing the placeholder with your bot's public
   username (with or without `@`).
3. Paste the new token only at its hidden prompt. It checks the sender using
   Telegram's read-only `getMe` method and stops if the username differs. It then
   prints candidate numeric chat IDs and a small identifying label, never
   tokens or message contents.
4. Choose the intended recipient yourself. Privately edit both
   `QT_TELEGRAM_BOT_TOKEN` and `QT_TELEGRAM_CHAT_ID` in `.env.orbstack`, then run
   `up` again. Leave both blank to keep alerts disabled.

After verifying the sender, the helper reads `getWebhookInfo`. An existing webhook stops discovery;
it never deletes or changes it. It does not send a message, set an update offset,
change the update filter, save the token, or print raw API errors. An active
poller/conflict is reported without changing that receiver. If another receiver
already consumes updates, use its recorded `message.chat.id` instead. Multiple
candidates require your own choice; a bot's username is not your chat ID.

## Data, resources and health

Named volumes hold research SQLite WAL/artifacts, Parquet data, and monitor
state separately. All services run as UID/GID 10001 with read-only roots,
dropped capabilities, bounded logs and tmpfs scratch space. Both images create
matching UID-owned volume mount points, including the shared read-only monitor
projection, so first creation order does not leave a root-owned volume.

API/research worker share the existing SQLite metadata store. The worker is
concurrency-one; the default total memory ceilings are about 3.5 GiB. Leave
headroom in OrbStack for builds and other projects. A successful resource
admission is not a guarantee against a container's cgroup memory limit.

No market history is fabricated. A fresh dataset volume is empty; import/sync
real data in the management page before running research. The image includes
the existing pinned Nautilus/Optuna/TA-Lib runtime. User-uploaded isolated Python
plugins require an independently configured plugin runner; this bundle does
not mount a privileged host Docker socket to make them run. Such jobs fail
closed rather than executing user code in the API process.

Health probes are quiet, credential-safe and bounded. Worker health checks its
own recent heartbeat or active attempt lease; the monitor reports process/feed
readiness separately. An unavailable exchange or VPN can leave the monitor
alive but market data unready. Healthchecks report status; Docker restart
policies restart exited processes, not every unhealthy running container. Mac
sleep, OrbStack being stopped, and network outages interrupt continuous data.

## Verification and troubleshooting

- Run `bash deploy/orbstack.sh check` to validate configuration, internal API and
  worker probes, and unauthenticated UI/API 401 responses.
- Inspect `status` first if startup's `--wait` fails. Diagnose only this
  project's logs, with secrets redacted, and check exchange reachability.
- A missing `.env.orbstack`, blank login, partial Telegram pair, unsupported
  source pair or permissive file permissions fails before startup.
- To back up, stop this project first, then export its named volumes through
  OrbStack. Use actual names from `docker --context orbstack volume ls`; never
  substitute the Finder `~/OrbStack/docker/volumes/...` path as a Docker mount.

Offline checks cover private-file handling, safe hidden-input discovery,
webhook/conflict behavior, header configuration, Compose isolation and worker
health. The native dependency lock's binary downloads were verified for
CPython 3.12 on Linux ARM64 and AMD64. Actual container build, auth routing and
fresh-volume behavior must additionally pass on the target OrbStack installation;
wheel availability and YAML parsing alone are not a runtime pass.

### Official references

- [OrbStack Docker / Compose and architectures](https://docs.orbstack.dev/docker/)
- [Volumes and mounts](https://docs.orbstack.dev/docker/file-sharing)
- [Container network and direct access](https://docs.orbstack.dev/docker/network)
- [Compose startup health order](https://docs.docker.com/compose/how-tos/startup-order/)
- [Caddy Basic Auth](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
- [Telegram update discovery](https://core.telegram.org/bots/api#getupdates)
- [Telegram sender identity](https://core.telegram.org/bots/api#getme)
- [Nautilus supported native platforms](https://nautilustrader.io/docs/latest/getting_started/installation/)
