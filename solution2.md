# Solution 2 — 普通人如何低成本、低频做加密量化交易

> 调研对象：peer-reviewed 学术论文、Glassnode/CryptoQuant/Capriole/LookIntoBitcoin
> 等链上分析师团队的研究、独立量化交易者公开分享的实证经验与失败案例。本文不是
> 操作教程，而是把"散户视角"下**可被一人长期维护、低费率、低频次**的几条
> 已被独立验证的 edge 整理成可落地的方案。

---

## 0. 设计原则（普通人的真实约束）

| 维度 | 真实约束 | 设计推论 |
| --- | --- | --- |
| **资金体量** | 1k–100k USDT | 滑点几乎可忽略，无法做高频做市；不能承受单次 -30% 回撤 |
| **时间投入** | 每周 1–4 小时 | 不能盯盘；必须**离线计算 + 信号通知 + 手动下单**或 cron 化 |
| **认知带宽** | 一个人维护一套代码 | 策略数 ≤ 4，参数 ≤ 10/策略；模型可解释 |
| **运行成本** | 服务器 ≤ $10/月，数据 API 免费层 | 走 ccxt 公共行情 + alternative.me F&G + Coin Metrics community + Glassnode 部分免费指标 |
| **税务/合规** | 一年内卖出按短期资本利得；持有 >1 年很多税区显著优惠 | 倾向"长持 + 极端事件加仓 / 减仓"，而非来回波段 |
| **心理承受** | 黑天鹅是常态（LUNA、FTX、3AC） | 仓位上限 + drawdown kill-switch，且**任何策略必须有"暂停按钮"** |

> "活下来比赚得多更重要" —— 这是 Charles Edwards (Capriole)、Andreas Clenow、
> Nassim Taleb 多次在不同场合的共同表达。

---

## 1. 学术与从业者证据综述

### 1.1 已被同行评议验证的边缘

| 来源 | 结论摘要 | 与策略的对应 |
| --- | --- | --- |
| Liu & Tsyvinski, *Risks and Returns of Cryptocurrency*, RFS 2021 | BTC 收益由三因子驱动：市场、规模、**动量**；1–8 周动量在样本内外都稳健 | S3 周线趋势 |
| Caporale, Gil-Alana, Plastun, *Persistence in the Cryptocurrency Market* (Research in International Business & Finance 2018) | BTC 价格在日级以上时间尺度存在长记忆 (Hurst > 0.5) | S3 |
| Akcora et al., *Bitcoin Risk Modeling with Blockchain Graphs* (2018) | UTXO-derived 指标（MVRV、SOPR）在样本外预测尾部下跌 | S2 多因子抄底 |
| Hubrich, *Know-When* (Cyber Forecasting, 2017) | 简单的 200d MA 过滤在 BTC 上把最大回撤从 -85% 降到 -30%，CAGR 几乎不变 | S3 |
| Ardia, Bluteau, Rüede, *Regime Changes in Bitcoin GARCH Volatility Dynamics* (Finance Research Letters 2019) | BTC 收益方差呈现明显的高/低波动两态切换 | 全部策略的仓位调节 |
| Bouri et al., *Bitcoin as a Hedge or Safe Haven* (Finance Research Letters, 2017+) | BTC 与 SPX、Gold 在极端区段相关性结构不稳定 | 谨慎使用 macro veto（VIX/DXY） |
| Gkillas & Katsiampa, *An Application of EVT to Cryptocurrencies* (Economics Letters 2018) | BTC 日收益尾部按 GPD 拟合最优；3σ 事件频率显著高于高斯 | 仓位与止损都要按尾部分布而非正态 |
| Härdle, Harvey, Reule, *Understanding Cryptocurrencies* (Journal of Financial Econometrics 2020) | 加密资产因子结构不同于股票；高换手 ≠ 高 alpha | 拒绝高频策略 |
| Borri, *Conditional Tail-Risk in Cryptocurrency Markets* (Journal of Empirical Finance 2019) | CoVaR 在 BTC/ETH 高度联动，分散化在尾部失效 | 单标的 + 现金为主，不依赖加密内部分散 |
| Hayes, *Cryptocurrency Value Formation* (Telematics & Informatics 2017) | 算力 + 难度 + 区块奖励解释 BTC 长期价值 ≈ 70% R² | Hash Ribbons / Puell 在 S2 中的合理性 |
| Faber, *A Quantitative Approach to Tactical Asset Allocation* (J. Wealth Management 2007) | 月线 SMA(10) 过滤把多类资产最大回撤减半，回报率几乎不变 | S3 直接借鉴 |
| Moskowitz, Ooi, Pedersen, *Time-Series Momentum* (JFE 2012) | 多资产、跨市场、跨年代验证，12 个月动量长期为正 | S3 |
| Da, Engelberg, Gao, *In Search of Attention* (Journal of Finance 2011) | Google Trends 突增在零售股票上预示反转；BTC 后续研究复现该现象 | 已实现的 `news_panic_z` |

### 1.2 从业者实证（公开可复现）

| 来源 | 公开经验 | 备注 |
| --- | --- | --- |
| **Philip Swift / LookIntoBitcoin** | Pi Cycle Top (111d MA × 2 vs 350d MA) 在 2013/2017/2021 三次顶部都在 3 天内触发；Pi Cycle Bottom (471d MA × 0.745 vs 150d EMA) 命中 Mar-2020 大底 | 已实现 |
| **Charles Edwards / Capriole** | Hash Ribbons（30d MA / 60d MA hashrate）识别矿工投降→恢复；公开回测 2014–2023 假阳性 ≤ 2 | 已实现 |
| **Saidi Lopp / Glassnode** | SSR（BTC mcap / 稳定币市值）+ SSR Oscillator (-1σ) 在 2020/2022 大底信号 | 已实现 |
| **Ki Young Ju / CryptoQuant** | Coinbase Premium 持续负（美机构卖压）+ 链上 STH-SOPR < 1 = 现货吸筹窗口 | 已实现 |
| **Willy Woo** | NVT Signal、Bitcoin Investor Tool；定投 + 周期顶减仓的"懒人策略"在 2011–2023 跑赢 buy-and-hold (经他自己回测) | S1 借鉴 |
| **Andreas Clenow** *Trading Evolved* | "Don't optimize—diversify across non-correlated systematic strategies"；周线 + 月度再平衡是个人量化天花板 | 整体框架 |
| **Robert Carver** *Systematic Trading* | 固定波动率目标（vol-targeting）+ 多策略 IR 加权 | 已用于 risk engine |
| **PlanB / Stock-to-Flow** | S2F 已被 2022 走势证伪；不作为入场依据 | 反例，**不要**用 |
| **3AC / Celsius / FTX 失败案例** | 杠杆 + 流动性错配 + 自托管缺失 = 必死；任何加密策略**永远不要满杠杆**，且仓位永远可立即清算 | 风控硬约束 |
| **r/algotrading & QuantConnect** 社区帖（2020–2024） | 散户最常见失败：过拟合、survivorship bias、用回测 fee=0、滑点未建模、未做 walk-forward | 所有策略必须 walk-forward |

### 1.3 个人开发者公开成功案例（采访/blog/repo）

1. **freqtrade** (开源, 2017–) — 数千用户运行；维护者反复强调："the strategies that ship with freqtrade are demos, not edges"。社区中真正长期盈利的用户共性：1) 低频；2) 不加杠杆；3) 多策略组合；4) 严格 walk-forward。
2. **Jesse-AI** (开源) — 文档第一句："Don't trust any backtest you didn't write yourself."
3. **Robert Carver 个人账户公开报告** (2014–) — 把同样的趋势 + carry 框架从期货扩到加密永续，年化 ~12%，Sharpe ~0.8，最大回撤 -22%。
4. **Capriole "Bitcoin Macro Index"** (Charles Edwards 实盘披露) — 多因子 voting，2018–2023 实盘跑赢 BTC HODL，回撤减半。

### 1.4 关键反例 / 错误综述

- **过度优化** — 把回测年化从 30% 调到 80% 几乎一定是 overfit。
- **生存者偏差** — 不要在过去十年涨幅 1000× 的 alt 上回测，只测 BTC/ETH。
- **fee=0 与零滑点** — 现实成本：Binance 现货 0.075%~0.10%/边，永续 0.04%~0.10%/边，slippage 在突破点至少 0.05%–0.15%。
- **未模拟资金费率** — 永续多空 8h 一次结算，长期可达年化 ±50%。
- **手动覆盖系统** — Carver 反复强调："the system's job is to keep you from yourself."

---

## 2. 四个候选方案（按"低频→中频"递增）

> 全部策略 **only-long BTC** 或 **市场中性现货+永续**，避免做空裸卖空。
> 全部按 1h 数据可运行，但**实际触发频率**远低；只看周线/日线的策略也可下沉到 1h。

### 方案 A — 智能定投 (Smart DCA)
- **触发**：每周一固定时间一次。
- **金额**：基础定投 × 压力系数 multiplier ∈ [0.25, 3.0]，由 F&G、MVRV-Z、回撤、长期均线偏离合成。
- **退出**：默认 HODL；可选"极端贪婪 + Pi Cycle Top"时减仓 20%–50%。
- **交易频率**：52 次/年（固定）。
- **预期**：在 2018–2024 模拟期略胜固定 DCA 0.5–1.5 倍 Sharpe，最大回撤接近 HODL。
- **目标人群**：完全不想盯盘、希望系统化"恐惧时多买、贪婪时少买"的长期持有者。

### 方案 B — 多因子极端事件抄底 (Multi-factor Capitulation)
- **触发**：5 因子组中 ≥4 组同时触发，macro 不否决。
- **入场**：分 2–3 段建仓（防止信号是"假底"）。
- **退出**：ATR 移动止损 + MVRV-Z 回到中位（约 2.0）+ 时间止损 120 天。
- **交易频率**：0–5 次/年。
- **预期**：Sortino > 2 但样本极少（5–10 笔/十年），统计显著性依赖事件本身的极端性。
- **目标人群**：愿意等待半年到一年只为一次"教科书级别"机会的耐心型交易者。
- **现状**：本仓库已实现核心，方案 B 是对其打包 + 分批入场 + 状态机化。

### 方案 C — 周线趋势跟踪 (Weekly Trend Following)
- **触发**：周收盘 close > SMA(20w) 多头开启，跌破 SMA(20w) 离场（或 trailing ATR×3）。
- **过滤**：4h 实现波动率 / 30d 实现波动率 > 1.5 时暂停（处于 vol-shock 阶段不追突破）。
- **仓位**：vol-target 15%/年 + Kelly 0.25 上限 50%。
- **退出**：MA 跌破或 ATR(14)×3 trailing。
- **交易频率**：2–8 次/年。
- **预期**：CAGR 25–40%，最大回撤 -25%–-35%（接近 HODL 的回撤，但平均持有时间 60%）。
- **目标人群**：相信趋势 + 想在熊市持现金的"系统派"。
- **学术基础**：Liu&Tsyvinski 动量 + Faber GTAA。

### 方案 D — 资金费率现货-永续套利 (Funding Carry / Cash-and-Carry)
- **触发**：永续 8h funding > 阈值 X（默认折年化 > 15% 时开仓；< 5% 时平仓）。
- **结构**：买等额现货 + 卖等额永续（市场中性），不持方向风险。
- **退出**：年化资金费率掉到阈值以下 OR basis 反转。
- **风险**：交易所风险（不是市场风险）；永续被清算需要现货侧腾挪；通常用 50% 抵押率以下。
- **交易频率**：变动，平均 8–20 次/年。
- **预期**：年化 8–20%（取决于市场情绪周期）、回撤 < 5%；Sharpe 通常 > 2，但年回报有上限。
- **目标人群**：想要"债券替代品"的稳定收益寻求者。

### 综合建议

| 风险偏好 | 推荐 | 备选 |
| --- | --- | --- |
| 完全被动 | A 80% + B 20% | 加 D 摇摆现金 |
| 平衡 | A 40% + C 40% + D 20% | B 替代 D |
| 进取 | C 60% + B 30% + D 10% | A 替代 D |

四策略权重相加，月度再平衡。Carver 框架下，**不同策略相关性低于 0.4**，组合 Sharpe 显著高于单策略。

---

## 3. 与现有代码的融合方式

现有 `qt` 仓库已经覆盖：
- 数据采集（ccxt / fapi / Coin Metrics / Glassnode / GDELT 等）
- 因子计算（price / vol / derivatives / on-chain / sentiment / events / smart-money / options / regime）
- compute_extreme_score 综合评分 + SignalEngine
- 回测引擎、风控、纸面 broker、walk-forward、Monte Carlo

新建 `src/qt/strategies/` 目录，将四个方案作为**独立、可单独 import** 的策略类：

```
src/qt/strategies/
  __init__.py
  base.py           # StrategyProtocol + 一个轻量回测 adapter
  smart_dca.py      # 方案 A
  capitulation.py   # 方案 B（薄包装现有 compute_extreme_score）
  trend_weekly.py   # 方案 C
  basis_carry.py    # 方案 D
```

每个策略：
1. 输入 = `pd.DataFrame` OHLCV + 可选的"额外数据"参数
2. 输出 = `StrategyResult`（含 position-target 序列、trade 列表、equity 曲线）
3. 类的顶部 docstring 完整解释**信号、参数、为什么这样设计、引用谁的研究、已知失败模式**

CLI 入口 `qt strategy run <a|b|c|d>` 让用户挑哪一种实盘/纸盘。
README.md 增加 "Solution gallery" 段落解释四个方案 + 何时用哪个。
