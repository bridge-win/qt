# 开源量化框架调研（2026-07）与 QT 升级计划

_调研日期：2026-07。定位：个人量化交易场景下的机会发现、高频交易、低频交易、
回测、机会建议与解释。本文先给出全网开源生态全景，再对照 QT 现状给出
升级建议与分阶段计划。配套阅读：[`ROADMAP.md`](ROADMAP.md)、
[`RESEARCH-EARNING.md`](RESEARCH-EARNING.md)。_

---

## Part 1 — 2026 开源量化生态全景

### 1.1 回测框架

| 项目 | 定位 | 2026 状态 | 对 QT 的相关性 |
| --- | --- | --- | --- |
| [vectorbt](https://github.com/polakowo/vectorbt) | NumPy+Numba 向量化回测，整段历史一次算完，参数网格扫描秒级 | 开源版进入社区维护（修 bug + 新 Python 版本）；新特性在闭源 [VectorBT PRO](https://vectorbt.pro/)（$20/月） | ★★★ 参数扫描 / walk-forward 比自研循环快 1–2 个数量级 |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Rust 内核 + Python API 的事件驱动回测/实盘一体框架 | 活跃，月度发版；Binance（现货+合约）、Bybit 官方 adapter 支持实盘行情与下单 | ★★☆ 回测→实盘同一套代码；但学习曲线陡、抽象重 |
| [hftbacktest](https://github.com/nkaz001/hftbacktest) | 高频/做市专用回测：L2/L3 全量 tick、订单队列位置、feed/order 双向延迟建模 | 活跃（Rust+Python），自带 Binance/Bybit 实例，同一策略代码可回测+实盘 | ★★★ wick catcher 这类深挂单策略的成交假设只有它能校验 |
| [backtesting.py](https://github.com/kernc/backtesting.py) | 单文件轻量回测，适合教学与草图 | 维护中，功能刻意保持小 | ★☆☆ 不如现有自研引擎 |
| backtrader | 上一个十年的零售标准 | 2023 年起长期维护模式，无新特性 | ☆☆☆ 不建议引入 |
| zipline-reloaded | Quantopian 遗产的社区续命版 | 维护中，偏股票日频 | ☆☆☆ 与加密场景不匹配 |

2026 年社区共识的工作流是**两段式**：向量化引擎（vectorbt）做高吞吐信号
探索与参数稳健性测试，事件驱动引擎（NautilusTrader / 自研 / hftbacktest）
做成交语义、滑点、延迟的真实性复核。
（参考：[python.financial 2026 综述](https://python.financial/)、
[BullAlert 对比](https://bullalert.ai/blog/best-python-backtest-engines-2026/)、
[autotradelab 对比](https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded)）

### 1.2 加密实盘机器人（低频 + 做市）

| 项目 | Stars（约） | 定位 | 值得借鉴的点 |
| --- | --- | --- | --- |
| [Freqtrade](https://github.com/freqtrade/freqtrade) | 25k–48k（口径不一），第一名 | Python 全功能 bot，30+ 交易所，2026.3 版本 | **FreqAI**：内置在线训练/漂移检测的 ML 信号管线；hyperopt 参数优化；Telegram 控制面 |
| [Hummingbot](https://github.com/hummingbot/hummingbot) | 6k–18k | 做市/HFT 框架，50+ CEX/DEX 连接器，v2.13（2026-03） | 做市与跨所套利的执行器抽象（V2 Strategy Controllers）|
| [Jesse](https://github.com/jesse-ai/jesse) | ~7.6k | 加密策略研究+实盘，强调无前视偏差 | JesseGPT（AI 助手写/解释策略）、简洁策略 API |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | ~5.4k | 面向非程序员的 bot | 云托管 + 网页配置的易用性思路 |
| [vnpy](https://github.com/vnpy/vnpy) / [vnpy_evo](https://github.com/veighna-global/vnpy_evo) | 30k+ | 中文社区最大量化框架；evo 分支专攻加密 | 中文文档生态；事件引擎 + 应用模块化（nova_strategy 趋势/配对/多因子） |

（参考：[Gainium 2026 开源 bot 评测](https://gainium.io/best/open-source)、
[CoinCodeCap 2026](https://coincodecap.com/open-source-trading-bots-on-github)）

### 1.3 AI / 因子研究平台

| 项目 | 定位 | 2026 状态 |
| --- | --- | --- |
| [Qlib](https://github.com/microsoft/qlib)（微软） | AI 量化研究全流程：数据→因子→模型→组合→执行；监督学习/概念漂移/RL | 活跃；crypto 需自行转数据格式（社区扩展支持） |
| [RD-Agent](https://github.com/microsoft/rd-agent)（微软） | LLM 自动化研发代理：自动提出因子/模型假设→写代码→回测→迭代进化 | 2026 年该方向最受关注的项目；与 Qlib 打通形成自动因子挖掘闭环 |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 强化学习交易框架 | 活跃，学术导向 |
| [QuantaAlpha](https://github.com/QuantaAlpha/QuantaAlpha) / [AlphaAgent](https://github.com/RndmVariableQ/AlphaAgent) | LLM + 进化策略自动挖掘 alpha 因子 | 新兴，验证中 |

### 1.4 LLM 交易代理（机会建议与解释）

| 项目 | Stars（约） | 定位 |
| --- | --- | --- |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 数万级（2026 年最火的 AI 交易框架） | LangGraph 多代理：基本面/情绪/新闻/技术分析师 + 多空研究员辩论 + 交易员 + 风控委员会；v0.3.1（2026-07）修复前视偏差过滤并支持 Claude Sonnet 5 |
| [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | ~59k | 14 个传奇投资人人格代理辩论选股（教学向） |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) / [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | 各数万 | 金融 LLM 平台 / 股研自动化 |

注意：学术基准（[StockBench](https://arxiv.org/pdf/2510.02209)、
[LiveTradeBench](https://arxiv.org/pdf/2511.03628)、
[Look-Ahead-Bench](https://arxiv.org/pdf/2601.13770)）对"LLM 直接交易能否赚钱"
结论普遍谨慎，且前视偏差是这一类框架的头号坑。**LLM 的可靠价值在
"解释、汇总、研究自动化"，而非直接下单**——这与 QT "信号可解释 + 人来决策"
的哲学一致。

### 1.5 数据与组合层

| 项目 | 定位 | 对 QT 的相关性 |
| --- | --- | --- |
| [cryptofeed](https://github.com/bmoscon/cryptofeed) | 多交易所 WebSocket 行情统一回调（trades / L2 book / funding / liquidations） | ★★★ QT 目前全靠 REST 轮询，wick/intel 缺实时推送 |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 开源金融数据平台，"connect once, consume everywhere"，自带 MCP server 供 AI 代理调用 | ★★☆ 宏观/跨资产数据可替代自拼的 FRED/GDELT 适配器 |
| [skfolio](https://github.com/skfolio/skfolio) | scikit-learn 风格组合优化/风险管理（[论文](https://arxiv.org/pdf/2507.04176)） | ★★☆ QT 五策略目前各自独立记账，没有组合层资金分配 |
| [ccxt](https://github.com/ccxt/ccxt) | 交易所统一 REST/WS API | 已在用，继续作为执行层基础 |

---

## Part 2 — QT 现状对照：哪里领先，哪里落后

**QT 已经做对、不需要推翻的**（很多自研件的质量高于同类开源默认值）：

- 策略哲学（低频、极端事件、N-of-K 因子投票、宏观否决）有文献支撑，
  这是"内容"，任何框架都替代不了；
- 多策略 runner + heartbeat + watchdog + systemd 部署 + 双语 dashboard
  的运维闭环，比 Freqtrade 单策略单进程模型更贴合本项目；
- 风控（¼-Kelly、vol-target、kill-switch、8 层实盘安全门）明确优于
  多数开源 bot 的默认设置；
- 可解释性（每个 Signal 携带触发因子）已经是 TradingAgents 想做的事
  的确定性版本。

**与 2026 生态对照的六个差距：**

| # | 差距 | 现状 | 生态最佳实践 |
| --- | --- | --- | --- |
| D1 | 参数稳健性验证吞吐低 | 自研 walk-forward/Monte Carlo 逐循环跑 | vectorbt 向量化网格：数千组参数秒级 |
| D2 | wick 类策略成交假设未经队列/延迟校验 | 自研 FillModel（成交价近似） | hftbacktest：L2 重建 + 队列位置 + 双向延迟 |
| D3 | 行情靠 REST 轮询（wick 1 分钟一拉） | ccxt REST | cryptofeed WebSocket 实时 trades/L2/liquidations 推送 |
| D4 | 机会"解释"是模板字符串 | intel 的 why/what-to-do 为固定文案 | TradingAgents 式 LLM 叙事：结合当下新闻/资金面生成个性化解释 |
| D5 | 因子研究靠人工 | thresholds_research.yaml 手工调 | RD-Agent 式自动因子假设→回测→迭代循环 |
| D6 | 五策略无组合层 | 各策略独立记账、独立限额 | skfolio：跨策略风险预算与资金分配 |

**明确不建议做的：**

- ❌ 整体迁移到 Freqtrade / NautilusTrader / vnpy——QT 的多策略+
  可解释+运维闭环重写代价远大于收益，且这些框架的策略模型与
  "N-of-K 因子投票 + 机会告警"范式不吻合；
- ❌ 让 LLM 直接下单（学术基准不支持，且违背 QT 安全哲学）；
- ❌ 转向日内高频做市作为主方向（ROADMAP Part 1 的证据：散户日内
  交易 97% 亏损；HFT 工具只用来**校验** wick 策略的成交现实性，
  不是转型方向）。

---

## Part 3 — 升级建议与分阶段计划

原则：**自研信号层是资产，保留；在数据、验证、解释三个薄弱面用
成熟开源件替换/增强自研件。** 每阶段独立可交付、可放弃。

### Phase 1 — 回测验证吞吐（vectorbt 研究层）＋ 成交现实性（hftbacktest 校验）

*预计 1–2 周，收益最大、风险最低。*

1. 新增 `qt.backtest.vector`：把 dca/trend/carry/wick 的信号规则译成
   vectorbt（开源版即可）向量化形式，用于参数网格 + walk-forward +
   随机化稳健性测试；自研事件引擎保留为"金标准"复核层，两边结果
   互相对账（同参数下指标差异 < 容差则通过）。
2. 用 hftbacktest + Binance/Bybit 公开 tick 样本回放 wick catcher 的
   深挂单阶梯：验证"挂单在队列中的真实成交率与成交价"，据此修正
   自研 `FillModel` 的保守系数。产出写进 `RESEARCH-EARNING.md`。
3. 交付物：`qt strategy sweep <name>`（参数热力图导出 dashboard）、
   wick 成交率校准报告。

### Phase 2 — 实时数据层（cryptofeed）

*预计 1 周。*

1. 新增 `qt.data.stream`：cryptofeed 订阅 trades / L2 / **liquidations** /
   funding，落地到现有 ParquetStore + 内存环形缓冲；REST(ccxt) 降级
   为补数与对账通道。
2. wick/intel 扫描器从"每分钟拉"升级为事件驱动触发——清算瀑布
   发生的那 30 秒内出告警，而不是下一个轮询周期。
3. 交付物：dashboard 数据源页显示 WS 连接状态与延迟；断线自动重连
   （沿用 tenacity）。

### Phase 3 — 机会解释与研究自动化（LLM，只读不下单）

*预计 1–2 周，依赖 Claude API key，可选。*

1. `qt.intel.narrator`：机会产生时，将触发因子、当时市场快照、近期
   新闻（GDELT 已有）交给 Claude 生成双语"为什么现在、历史类似
   情形、主要风险、建议动作"，替换模板文案；LLM 输出仅作为
   Opportunity 的附注字段，**不进入任何交易路径**。
2. 借鉴 TradingAgents 的多视角结构做轻量版：bull case / bear case /
   风险提示三段式，提示词中强制引用具体因子数值（防幻觉）。
3. 研究侧试点 RD-Agent 模式：每周离线跑一次"因子假设→vectorbt
   回测→报告"循环，产出进 `docs/`，由人审核后才改 thresholds。
4. 交付物：`/intel` 页每条机会带可折叠的 AI 解读；每周研究报告。

### Phase 4 — 组合层（skfolio）与长期演进

*预计 1 周 + 持续。*

1. `qt.portfolio.allocator`：用 skfolio 对五策略的 paper 收益序列做
   风险平价/HRP 资金分配，替代各策略独立固定限额；先只在 dashboard
   展示"建议分配 vs 当前分配"，人工确认后生效。
2. 长期观察项（暂不投入）：NautilusTrader 作执行内核（仅当策略
   数量/复杂度超出 ccxt LiveBroker 承载时）；Hummingbot（仅当决定
   认真做做市）；Qlib（仅当扩展到多币种截面因子）。

### 依赖变更汇总

```toml
# 新增（Phase 1–4 逐步引入，均为可选 extra）
research = ["vectorbt>=0.26", "hftbacktest>=2"]
stream   = ["cryptofeed>=2.4"]
ai       = ["anthropic>=0.40"]
portfolio = ["skfolio>=0.5"]
```

### 风险与回退

- vectorbt 开源版仅维护不加新功能：QT 只用其核心向量化回测，够用；
  若不足再评估 PRO（$20/月）或自写 Numba 核；
- hftbacktest 需要 tick 级数据（体量大）：只对 wick 做样本期校验，
  不做全历史回放；
- LLM 解释成本与幻觉：限流（仅机会触发时调用）、提示词强制引用
  因子数值、输出永不进入交易路径；
- 所有阶段互相独立，任一阶段失败不影响现有 84 个测试覆盖的主干。
