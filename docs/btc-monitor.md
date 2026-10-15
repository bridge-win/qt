# BTC 现货只读监控：部署与运维

多平台配置、测试执行与独立 Polymarket 只读适配见 [多平台说明](multi-venue.md)。

## 边界与架构

本组件采集 Binance 或 OKX **SPOT BTC/USDT** 公共行情（含明确区分的测试环境），识别短时异常、评估网格市场条件，并提供可过期的研究候选。`live_execution_enabled` 固定为 `false`；没有订单、账户查询、资金、持仓、交易密钥或实盘开关。不会调用现有 `qt.btc_agent` 的执行服务，也不共享它的数据库或执行锁。

部署只有一个独立采集 worker：

- `feed`：公共 WebSocket 成交、买卖一报价，以及已收盘的一分钟 K 线；REST 补充连续已收盘 K 线。
- `engine`：确定性的秒级路径/高低点、异常生命周期、成本与趋势等网格门槛；网络、通知及磁盘 I/O 不进入评估内核。
- `store`：一个写入线程持有 SQLite 与操作系统目录锁，保存有界决策/outbox，原子替换 `latest.json`。
- `notifications`：可选 Telegram 异步发送；不阻塞行情采集。
- 现有 workbench 的只读路由读取 JSON；现有页面轮询该路由。API 不启动采集，不创建/迁移监控数据库。镜像不运行 API，不暴露任何端口。

最小镜像只复制 `qt/__init__.py` 与 `qt/btc_monitor`，仅安装 `httpx`、`pydantic`、`websockets` 及其锁定的传递依赖。Python 3.12.11 基础镜像与 Dockerfile frontend 均固定 digest；下载和离线安装两阶段都校验 SHA-256。它不安装整个 QT 的回测、broker、研究或前端依赖。

复用当前 QT 已有的开源技术栈：运行依赖固定为 [HTTPX 0.28.1](https://github.com/encode/httpx)、[websockets 16.1.1](https://github.com/python-websockets/websockets) 和 [Pydantic 2.13.4](https://github.com/pydantic/pydantic)，版本及分发哈希见 `requirements-btc-monitor.lock`。HTTPX 提供异步 HTTP 客户端与有界超时，websockets 负责 WebSocket 协议/缓冲/连接管理，Pydantic 校验配置与消息投影；应用另外实现逐通道新鲜度、预热、持久化与看门狗。这些复用边界旨在减少自建协议代码，保持组件可测试；策略有效性仍需独立验证。

## 数据源与就绪条件

默认 REST 为 `https://data-api.binance.vision/api/v3`，WebSocket 为 `wss://data-stream.binance.vision:443/stream?streams=btcusdt@aggTrade/btcusdt@bookTicker/btcusdt@kline_1m`。这些是官方公开市场数据专用地址，不需要 API key，不支持用户数据流；参见 [Binance 官方说明](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md)。本实现没有 USD-M 适配器；现货价格不能解释成永续合约标记价、资金费或清算数据。

REST 使用 Binance Spot **API v3** 的 `GET /api/v3/klines`，完整地址为 `https://data-api.binance.vision/api/v3/klines`；查询参数固定 `symbol=BTCUSDT`、`interval=1m`，并传入有界 `limit`（默认启动请求 120 根）。仅接纳已经收盘且连续的 K 线。接口参数与时间语义见 [官方 REST 行情文档](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#klinecandlestick-data)。

健康状态分别表示：进程/核心是否前进、每个数据通道是否新鲜、以及评估历史是否足够。成交使用源事件时间、接收时间和消费积压延迟；不能用刚处理的旧成交刷新行情。报价流没有交易所事件时间，应显示 source-time-unknown，使用本地接收时间，不伪造源时间。一个报价通道的新数据不能掩盖成交停滞。协议与报价格式参见 [官方现货 WebSocket 文档](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md)。

默认交易与报价新鲜度各为 5 秒，收盘 K 线为 125 秒，消费积压上限 2 秒，未来时钟偏差容忍 2 秒。微观数据就绪至少覆盖 60 秒且有 45 个成交秒桶；网格采用独立的 900 秒观察窗，至少积累 600 秒实时覆盖及 450 个成交秒桶，不能只凭一分钟微观历史给出网格候选。历史 K 线不能替代实时成交覆盖。重连、丢失覆盖或队列溢出后，受影响的候选失效并重新预热。连接失败或必要通道静默采用带抖动的退避重连，默认每 23 小时主动轮换会话；REST 补历史失败仍会尝试独立 WebSocket 采集。服务器需有可靠的 UTC 时间同步。

异常有稳定 event ID 和递增 revision，并在 WATCH、CONFIRMED、INVALIDATED、EXPIRED 间变化。研究候选使用短 TTL，默认评估有效期为 5 秒；页面/API 会重新判断过期和数据健康，不能因旧 JSON 或页面缓存继续显示新鲜候选。

## 网格结果如何理解

本里程碑只有命名为 `balanced` 的均衡灵敏度预设，代表当前默认启发式门槛；没有额外档位，也没有经过经验校准或盈利验证。页面显示该预设及配置版本，修改 JSON 参数会改变配置哈希，不会赋予执行权限。价格步长暂假设为 `0.01 USDT` 并明确标记 `tick_size_assumed`；未核验交易所实时交易过滤器/数量与金额约束，不能把研究层数和区间直接变成订单。

CANDIDATE 只表示当前市场满足启发式研究门槛，不是成交概率、盈利保证或可执行订单。门槛包含覆盖率、双向运动、趋势/突破风险、价差和净格距，页面同时显示实测值、阈值与拒绝原因。

默认成本假设为每边手续费 10 bps、每边滑点 2 bps，另加 3 bps 缓冲；往返费用、滑点及缓冲合计为 27 bps，评估还会加上实测买卖价差；净格距必须严格大于零。实际费率等级、预算、可卖 BTC 库存及交易所订单金额/数量约束均未知。即使市场条件合格，也仅允许 monitor/research，不给出资金配置、下单数量或执行权限。跨交易所确认、策略盈利能力等未验证项会明确列为缺失输入。

## 状态目录与权限

直接运行的默认目录为 `data/workbench/btc-monitor`。workbench API 固定读取它自己的 `state_root / "btc-monitor"`；例如现有 systemd 配置的 `/opt/qt/data/workbench/btc-monitor`。Compose 默认将仓库中的同一目录绑定到容器 `/state`。自定义 workbench `--state-root` 时，必须同时设置 `QT_BTC_MONITOR_HOST_STATE_DIR`，指向同一主机目录，避免两个独立数据仓库。

**先由运维创建目录，不能让 Docker 自动创建 root 所有的目录。** Compose 使用 `create_host_path: false`，不存在时应失败。仅一个 worker 可以拥有该目录；不要扩容副本或同时运行宿主机 worker。锁文件 `.writer.lock` 的存在本身不表示服务仍在运行，真正排他性由进程持有的系统锁决定，不要依赖删除锁文件恢复服务。

以下为 Linux 主机准备示例，在部署目录执行；将 `qt` 换成实际 workbench API 用户。数值 UID 10001 专供容器写入，目录的 setgid 保证新文件继承 API 可读的组。不要递归更改整个 workbench、交易数据或其他服务的所有权。

```sh
cd /opt/qt
API_GROUP=$(id -g qt)
sudo install -d -o 10001 -g "$API_GROUP" -m 2750 data/workbench/btc-monitor
cp config/btc-monitor.env.example .env.btc-monitor
chmod 600 .env.btc-monitor
sed -i "s/^QT_BTC_MONITOR_READ_GID=.*/QT_BTC_MONITOR_READ_GID=$API_GROUP/" .env.btc-monitor
# 确认 QT_BTC_MONITOR_HOST_STATE_DIR 对应实际 state_root/btc-monitor。
```

容器以 UID/GID `10001:10001` 运行，并加入指定的只读 API 组。宿主机目录的所有祖先也要允许容器 UID/补充组和 API 用户穿越；仅对目标目录及必要祖先设置最小权限。workbench 只需要读取 `latest.json`；不要将 Telegram token 放入状态目录或 JSON。现有目录迁移时应先停止旧 writer，再核对目标文件属主、组及投影可读性。

## 部署后手动启动 Docker

本说明不代表已经部署。需要支持长语法 bind mount 的 Docker Compose v2、Docker daemon、允许访问 Binance 的合规网络和足够磁盘空间。不要通过换域名、代理等方式绕过服务商地区或访问限制。

推送 `main` 会触发现有 `Deploy TC` 流程，自动部署研究 workbench 的 UI/API 等现有服务；它不会构建、安装或启动这个新监控容器。当前发布归档仅包含 `src`、`scripts`、`config`、`packages`、项目元数据与前端构建产物，不包含本监控的 `Dockerfile.btc-monitor`、Compose 文件、依赖 lock 或本运维文档。因此，仅等待现有线上发布成功不足以完成监控部署；合规目标主机还需通过完整仓库 checkout 或经审核的完整构建材料取得这些文件，再按下列运维步骤手动启动。无监控时，新页面只会显示快照不可用，其他 workbench 页面继续工作。

在上述准备完成、完整构建材料到位且代码部署并审核后，由运维执行：

```sh
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml config --quiet
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml build
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml run --rm --no-deps monitor self-test
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml up -d monitor
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml ps
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml exec -T monitor python -m qt.btc_monitor status --state-dir /state
docker compose --env-file .env.btc-monitor -f docker-compose.btc-monitor.yml exec -T monitor python -m qt.btc_monitor health --state-dir /state
```

`self-test` 始终自行创建并删除临时目录，不连接 Binance 或 Telegram，忽略 `QT_BTC_MONITOR_CONFIG`、`QT_BTC_MONITOR_STATE_DIR` 及 CLI `--state-dir`，不会把模拟快照写入生产状态；测试结束不留下供 `status` 读取的投影。`status` 打印诊断 JSON；`health` 的退出码用于核心进展检查。停止时执行同一 Compose 命令的 `down`；绑定的主机数据保留。

`restart: unless-stopped` 在进程退出及 daemon 重启后按 Docker 的规则恢复容器；主机需配置 Docker daemon 随开机启动，手动停止的容器不会因此重新启动。单独的 unhealthy 状态不会触发 Docker 重启，参见 [Docker 官方重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)。不要再用另一套主机进程管理器同时拉起该 worker。

Compose 启用 init、只读根文件系统、丢弃所有 capabilities、no-new-privileges 和 32 MiB 临时文件系统；只允许 `/state` 持久写入。初始资源上限为 1 CPU、384 MiB 内存、64 PID；这是部署保护，不是已经完成容量压测的结论。日志最多 3 份、每份 10 MiB。优雅停止窗口 20 秒，修改应用关闭期限时也要相应调整它。

## 可选配置与 Telegram

公共监控完全不需要密钥。Telegram 仅识别已有名称 `QT_TELEGRAM_BOT_TOKEN` 与 `QT_TELEGRAM_CHAT_ID`；留空即可禁用。部署后手动填写私有 `.env.btc-monitor`，不要提交该文件、把值写进命令行/截图、或公开 `docker compose config` 的完整展开输出。Compose 的 environment/容器检查对有 Docker 权限的操作者可见，不能当作独立保险库。

通知使用 HTTPS Telegram Bot API：`POST https://api.telegram.org/bot<token>/sendMessage`，请求包含目标 `chat_id` 和通知 `text`。`<token>` 只是路径占位符，绝不能写入实际值到文档、日志或投影；该 endpoint 未包含 API 版本号，本实现不把它描述成固定版本 API。参数与响应语义见 [Telegram 官方 sendMessage 文档](https://core.telegram.org/bots/api#sendmessage)。

应用可通过 `QT_BTC_MONITOR_CONFIG` 读取无秘密 JSON 配置，未知字段和非法数值会被拒绝。直接 Python 的优先级为 CLI `--state-dir` 高于 JSON `state_dir`，JSON 高于 `QT_BTC_MONITOR_STATE_DIR`。默认 Compose 的 run/health 均显式固定 `/state`。若需修改门槛，另用 Compose override 挂载只读 JSON 并设置容器内路径，例如：

```yaml
services:
  monitor:
    environment:
      QT_BTC_MONITOR_CONFIG: /etc/qt/btc-monitor.json
    volumes:
      - type: bind
        source: ./config/btc-monitor.json
        target: /etc/qt/btc-monitor.json
        read_only: true
        bind:
          create_host_path: false
```

配置中不要放 Telegram 值、认证 URL 或执行密钥。修改配置后重建/重建容器前先离线验证；不要把默认门槛当作回测校准结果。现有 QT 管理界面与该组件没有新增密钥同步机制；不能假定在其他模块设置的 token 会自动注入此容器。

Telegram 使用持久 outbox，以 event ID/revision/通知类别去重，发送前重新检查最新修订和有效期；已失效、撤销或过期的正向机会不会在恢复后补发为当前机会。429 按 retry-after 进行有界重试，超过有效期或尝试上限即停止。请求超时或进程在发送中退出，可能发生 Telegram 已接受但本地未拿到结果：本实现记录 unknown，避免自动重试不确定结果，因此可能漏发。它不能承诺外部 exactly-once；accepted 只表示 Telegram API 接受，不代表手机送达、已读或交易所动作。

## 健康、恢复与保留

Docker health 检查最近快照、核心进展及持久化进展，不以交易所短时断线或未配置 Telegram 直接判定进程死亡。普通市场故障应显示数据未就绪并退避重连，不能因外部断线重启风暴。内部无法恢复的存储/队列/核心故障会停止 worker。独立于 asyncio 的进度 watchdog 检查消费、评估及持久化；默认核心进展停滞 30 秒时请求停止，关闭期限后仍卡住则强制退出，让 Docker 重启策略接管。它不因市场断线本身退出，持续 stale/unhealthy 或反复退出仍需运维关注。

API 每次读取重新检查投影版本、1 MiB 大小上限、10 秒快照年龄、2 秒未来偏差、通道和评估 TTL；通道年龄上限为成交/报价各 5 秒、收盘 K 线 125 秒，积压上限 2 秒。配置只可让读侧采用更严格的通道/积压门槛，不能放宽这些安全上限。缺失、损坏、未来时间或陈旧快照会呈现 unavailable/degraded，正向候选被压制；前端使用单调时钟累计有效期，将完整请求往返耗时计入新鲜度；本地时钟回拨、页面重挂载、悬而未决或失败的轮询均不能为缓存续期。轮询失败或展示到期会撤下候选。采集容器尚未安装不妨碍现有 workbench 的其他页面。

持久文件包括 `monitor.sqlite3`、`latest.json` 和 `.writer.lock`。SQLite 使用 DELETE journal 与单写入者；JSON 在同一目录临时写入、同步后原子替换。重启保留历史及 outbox，实时就绪必须重新预热，不能将磁盘历史当成新行情。

队列、历史、决策、outbox 和日志均有上界；持久化默认最多保留 1000 个决策及 1000 个 outbox 项，已结束项还受 7 天保留期约束。磁盘可用空间低于 16 MiB 时存储失败并停止；这不是完整磁盘配额，应由主机额外监测实际占用、日志及备份。交易路径溢出会破坏覆盖并阻止候选，不能悄悄丢成交后宣称数据完整。

备份可在 worker 停止后复制完整状态目录；在线一致性备份应使用 SQLite backup API，不能随意复制正在写入的数据库。恢复应在停机、确认没有第二 writer 后进行，并保留现场备份；不要直接删除数据库作为常规修复手段。

**必须由运维另外配置主机/外部心跳告警。** 当前 worker 或整台服务器停止时，它自身无法发送 Telegram 告警。外部监测至少应覆盖主机可达性、容器退出/持续 unhealthy、投影/核心进度时间、数据通道年龄与磁盘余量。本仓库未为该组件部署独立外部监控。

## 验证范围

本次开发已完成最小运行依赖的完整哈希下载、全新 Python 3.12 环境离线安装及 `pip check`，并通过 Compose YAML/镜像边界/状态挂载及 CI 接线的 7 项本地测试。按镜像 COPY 边界单独复制源码后，确认没有 FastAPI 或执行包仍可导入 CLI、通过临时目录离线 self-test；另用离线模拟 worker 验证 status 及快照模式 0640；模拟交易所不可用时核心持续前进，CLI health 为成功，优雅停止后 liveness 失效。静态 YAML 测试和这些宿主机模拟均不等于 `docker compose config`、镜像构建或容器验收。

现有 CI 已新增 `btc-monitor-image` 作业（push/PR/手动触发，排除已有定时触发），会在 Ubuntu Docker runner 上校验 Compose、实际构建 Linux amd64 镜像并运行 `scripts/ci_btc_monitor_image_smoke.sh`。所有运行期容器使用 `--network none`，不需要任何密钥；测试只读根目录、UID/capabilities/no-new-privileges、有界资源与临时文件系统、默认 CLI self-test，以及只读挂载的离线 feed fixture 注入真实服务后的 status/health。它还测试 setgid 状态文件/API 组只读访问、暂停核心后健康过期、恢复、重启计数及 SIGTERM 的正常退出/停止投影，并输出实际容器 Python/SQLite/核心依赖版本。fixture 不打入生产镜像，不改变生产 CLI。

已发布应用提交 `5b3ed76a67c9883a3b5142c646a95f8bdb2cef65` 的 [离线 Linux amd64 镜像构建与运行验收作业](https://github.com/bridge-win/qt/actions/runs/38684088279/job/116254869050) 已实际通过。容器报告 Python `3.12.11`、SQLite `3.40.1`（使用 DELETE journal）、HTTPX `0.28.1`、Pydantic `2.13.4`、websockets `16.1.1`。这项结果证明该提交的离线镜像构建及上述容器运行检查通过；同次 CI 另有 Python 3.10 回测频率元数据断言失败，不能据此宣称整条 CI 已通过。

本地开发环境没有 Docker，因此本地未执行镜像 build、容器启动、容器 UID/挂载/health 验证；本环境此前收到 Binance HTTP 451 地区限制，未继续做任何 Binance 实时探测，也未做真实 Telegram 发送。离线 fixture/self-test 和单元回归不证明服务器已部署、实时数据可达、手机送达、24 小时稳定运行或网格盈利。

正式验收需在合规目标主机完成 Compose 展开/镜像构建、只读与权限验证、无认证行情连接、逐通道停滞/断线重连/重启/磁盘故障测试、可选 Telegram 投递确认及至少 24 小时实际观察，并记录目标主机上的 Python/SQLite/依赖版本和资源占用。
