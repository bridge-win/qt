# Binance / OKX 测试环境与 Polymarket 公共数据

本次适配明确区分行情身份、测试执行与预测市场。没有实盘开关、交易密钥网页表单、钱包连接或 Polymarket 下单功能。离线合约测试不能替代真实认证、测试网成交、Docker 部署或 24 小时稳定性验收。

## 能力范围

- BTC 只读监测：Binance 现货 production / Spot Testnet，OKX 现货 production / demo。采集 BTC/USDT 现货成交、报价与已收盘 1 分钟 K 线。
- 测试执行：`qt.btc_agent` 保留 Binance Spot Testnet，新增 OKX demo。独立测试密钥、订单查回、成交核对与状态目录；没有生产交易路径。见 [BTC agent 说明](codex-btc-agent.md)。
- Polymarket：按 market slug 读取公开 Gamma 市场与各 outcome 的 CLOB 盘口。价格表示事件结果份额的市场报价/隐含概率，不是 BTC/USDT 现货，也不会进入 BTC 网格或插针引擎。
- Polymarket 仅 production-public 读取与本地 fixture 测试。当前没有核验可用的官方公共 sandbox 主机，因此不提供 testnet、签名或执行模式。SDK 的测试链编号不构成已验证的交易所测试环境。

## BTC 监测配置

`QT_BTC_MONITOR_VENUE` / `QT_BTC_MONITOR_ENVIRONMENT` 只有以下合法组合：

- `binance` / `production`（默认）：官方 Binance data-only REST / WebSocket
- `binance` / `testnet`：REST `https://testnet.binance.vision/api/v3`；WS `wss://stream.testnet.binance.vision/stream?...`
- `okx` / `production`：REST `https://openapi.okx.com/api/v5`；WS `wss://ws.okx.com/ws/v5/public` 和 `/business`
- `okx` / `demo`：同一官方 REST，明确携带 `x-simulated-trading: 1`；WS `wss://wspap.okx.com/ws/v5/public` 和 `/business`

不接受自定义主机、交叉环境端点或其他币种。OKX 使用默认 TLS 443，不使用将下线的 `:8443`。当前只适配全球官方端点；需要 US/AU/EEA 区域端点的账户尚未支持。禁止用代理或替换主机绕过地区限制。公开行情不需要交易密钥。

```sh
QT_BTC_MONITOR_VENUE=okx QT_BTC_MONITOR_ENVIRONMENT=demo \
  python -m qt.btc_monitor run
```

非默认环境自动使用独立目录，例如 `data/workbench/btc-monitor-okx-demo`。显式 `--state-dir` / `QT_BTC_MONITOR_STATE_DIR` 可覆盖，但每个目录只能绑定一个来源。旧快照属于其他来源时启动失败，保留旧投影，不发送旧通知。`status` / `health` 也核对所选来源，必须使用与 worker 相同的环境设置。

Workbench 使用 `scripts/run_workbench_api.py --btc-monitor-state-dir PATH` 读取相应目录；这不会启动 worker。界面动态显示 venue/environment，demo/testnet 有明显标识。

Docker Compose 继续使用 [加固运行方式](btc-monitor.md)。在私有 env 文件里设置上述两个非秘密选择器，并把 `QT_BTC_MONITOR_HOST_STATE_DIR` 改为该环境独立主机目录，预先创建并设置权限。容器内仍为 `/state`，healthcheck 继承环境。API 读取同一主机目录；同时启动多个环境时使用不同 Compose project 与挂载。

### 数据完整性

OKX `tickers` 使用 public socket；逐笔 `trades-all` 与 `candle1m` 使用 business socket。两条连接构成同一健康周期：任一断开、拒绝订阅或关键通道超时，都取消并等待另一条结束，清除就绪状态，然后重连。文字 `ping` / `pong` 不刷新市场通道健康。

使用 `tradeId` 去重，不能使用可重复的 `seqId`。本实现保守要求逐笔 ID 连续；不明跳号重新预热，不假定未丢失。报价使用 OKX 源 `ts`，重复时间戳不刷新健康。K 线仅接收 `confirm == "1"`，REST 逆序历史先排序并检查连续。OKX 单次 bootstrap 最多 300 根。

Assessment、异常事件、通知与顶层投影携带同一来源。测试和生产数据不混入同一个实时窗口。跨平台确认仍未配置；支持多个 venue 不代表已经交叉验证。

监测器价格步长仍明确标为假设：Binance 默认 0.01 USDT，OKX 默认 0.1 USDT（2026-10-15 官方公开 instruments 核验）。启动时可在非秘密 JSON 配置里显式设置 `tick_size`；部署前应重新核验交易所最新元数据。监测假设不替代测试执行 broker 每次缓存到期后的正式数量/价格约束检查。

## Polymarket

Workbench 独立卡片需要输入 market slug 后点击读取，没有默认轮询，不接受任意网址。CLI 同样只读一次：

```sh
python -m qt.prediction_markets --slug YOUR-MARKET-SLUG
```

API：`GET /api/v3/prediction-markets/polymarket/{slug}`，沿用 Workbench origin 身份验证。仅访问官方 Gamma `/markets/slug/{slug}` 和 CLOB `/book?token_id=...`。不跟随重定向，响应大小有界，不回退其他主机。

- 按明确 `version` 选字段：v1 使用 `clobTokenIds`，v2 使用 `positionIds`。缺少 version 仅兼容旧版 v1；未知版本拒绝。
- outcome 与 token 按数组下标映射，核对返回盘口的 asset ID 和 condition ID。
- 不假定盘口排序；bid 取最大值，ask 取最小值。空或单边盘口不伪造 midpoint，不从 YES 推导 NO。
- 超过 15 秒、未来、交叉、异常数字或身份不符不能就绪。第二个 outcome 的读取不会续期第一个。前端用单调时钟，缓存/重新挂载/失败/传输时间不能延长有效期。
- closed/inactive/archived/orderbook-disabled/not-accepting-orders 不读取盘口。`restricted` 是适用地区限制的元数据，单独警告；不表示当前用户已被禁止公开读取，也不是交易许可。实际 403/访问拒绝不绕过。

市场报价不等于预测结论、获利保证或可执行订单。没有钱包、签名、资金管理或真实交易。

## 验证

离线 fixture 覆盖端点/环境、demo header、两 socket 共同取消、文字心跳、来源与状态冲突、源时间、逐笔缺口、Gamma v1/v2、盘口身份/排序/时效、HTTP 拒绝/重定向/异常响应、API 认证边界和前端 TTL。

```sh
ruff check src tests
pytest -q
pytest packages/btc-backtest/tests -q -m 'not integration and not performance'
mypy src/qt/btc_monitor src/qt/prediction_markets src/qt/btc_agent
cd web && npm test && npm run build
```

密钥只能由使用者在自己控制的运行环境私下配置，不写聊天、代码或日志。暴露过的密钥应撤销并重新生成。认证连通性、测试网实际订单/成交、地区可达性、Docker daemon、Telegram 手机送达与 24 小时稳定性需在合规部署环境另行验收；离线测试不能证明这些事项已完成。

官方依据：[Binance Spot Testnet](https://developers.binance.com/en/docs/products/spot/testnet/general-info)、[Testnet WebSocket](https://developers.binance.com/en/docs/products/spot/testnet/web-socket-streams)、[OKX API v5](https://app.okx.com/docs-v5/en/)、[OKX changelog](https://www.okx.com/docs-v5/log_en/)、[Polymarket 市场版本](https://docs.polymarket.com/market-data/market-details)、[价格与盘口](https://docs.polymarket.com/market-data/prices-order-books)。
