# QT 个人交易 Agent：2026-10-22 独立调研与目标架构

调研核验日：**2026-10-22**。本文是独立复核，不覆盖 [2026-10-21 调研](industry-research-and-optimization-plan-2026-10.md)。信息只采用 2026-09 及以后一手来源，或至今仍生效的滚动规范（MCP 2026-07-28、A2A 1.0）。厂商案例不是收益证明。

仓库同步实现见阶段 A：执行合同、证据核验、有界研究循环、trial/forecast 账本、调度语义、记忆/RAG、可观测、统一 Hub UI。

---

## 0. 需求整理

两个目标互相拉扯：

- **个人金融交易 Agent**：低成本、少组件、paper 正确、可恢复、可对账；默认不实盘。
- **面试 Agent 架构师/专家岗**：能讲清企业完整生态（编排、记忆、RAG、技能、MCP、沙箱、trace、评测、调度、回测、治理），并用本仓库当证据。

处理原则：个人版只做合同正确的最小实现；每个合同对应一项可在面试展开的企业能力。不为展示堆框架。

---

## 1. 项目现状（2026-10-22）

QT 是三套并行运行时，不是单一产品：

- Workbench 8877：React 研究工作台 + Nautilus/Lab（最强个人生产面）。
- Agent Hub 8878：六角色固定流水线 + paper 账本（工程级 demo，`live_ready=false`）。
- Gallery 8765：策略线程 + PaperBroker。

准确定位：可审计、可恢复的多角色研究评审流水线。固定六角色在 2026 年不是缺点（单写者 + 多个只读贡献者）。缺的是研究阶段有界循环、副作用合同、统一 UI。

---

## 2. 同类金融量化 Agent（核验过的 2026-09+ 产品）

- **QuantConnect Agents / Mia**（文档 2026-10）：Ideas → Research → Validation(critic) → Backtest → Paper → Live Monitoring。LEAN 把回测诊断结构化回喂 Agent。实盘需显式 write grant。Paper 三门禁：按代码执行、paper 与同区间回测一致、OOS 落在回测包络内。Live Monitoring 只报对持仓不利的重大事件，不建议调仓。
- **Robinhood Agents**（HOOD Summit 2026-09-29）：独立 agentic account；内置审批默认开，外部 MCP `https://agent.robinhood.com/mcp/trading` 默认关；约 150k agentic accounts、30M tool calls/日；Loops 定时指令即将上线。
- **Perplexity Finance / TradingView Copilot**（2026-10）：研究层标来源与 as-of；不执行。
- **TradingAgents v0.7.0**（2026-10-16）：决策按**决策日后第一个收盘**计分，避免盘后新闻泄漏。这是 2026 开源金融 Agent 最重要的工程教训。
- **ai-hedge-fund v2.6.0**（2026-10-19）：回测与 paper 同一路径；hash 链账本；失败停机直到人工清除。
- **Heurist Finance / AWS AgentCore**（2026-09-09）：最接近本项目的企业金融工作台——身份、记忆、隔离 Code Interpreter、Tracing 贯穿。借鉴架构，不推导收益。
- **机构**：BlackRock 插件注册表；Bloomberg ASKB 引用来源并输出 BQL；Man Group 过与人类相同统计门槛；Morgan Stanley FIXR 刻意降低自主性；Two Sigma「明年不会是 LLM 下单」。

共性：审批默认开、资金隔离、阶段晋升、确定性算数字、LLM 只提案、append-only 账本、模型外 kill switch、时点纪律。

尚未被证明：持久真金 LLM alpha。Vals Finance Agent v2.1 top ~67% 只衡量研究。FARSIGHT：学术方案几乎都不在 LLM 外强制止损。

---

## 3. 企业 Agent 生态（2026-09+）

七层：渠道 → 身份 → 网关 → Harness → 持久执行/调度 → 隔离环境 → 记忆/知识 → OTel/评测/治理。

本轮核验：

- OpenAI Agents API 公测 2026-09-10：harness + environment + application server。循环放代码里。
- AWS AgentCore Runtime V2 GA 2026-09-18：快照冷启动 P75 1.9–2.0s；Memory/Gateway/Identity 拆开。
- Microsoft Foundry 2026-09-24：Routines GA（接受 ≠ 完成 ≠ 送达；模型不选收件人）；Tool Search GA；A2A GA。
- Anthropic Skills GA；web_fetch 只允许上下文已出现的 URL。
- Google 沙箱 pause-resume 保留文件系统；deferred tier 适合夜间复盘。
- 中国云：阿里云 FC Agent Sandbox（E2B 兼容）；腾讯云 ADP 4.0 Claw。

案例：Heurist（金融工作台）、Abnormal（规则→ML→Agent，困难样本才进 Agent）、Orange Business（三层评测）、Numan（生产同一 graph 评测阻断发布）、Morgan Stanley FIXR（更少自主）。

MCP 当前规范仍是 2026-07-28。Thoughtworks Caution：MCP by default。内部角色不要改成 A2A。

---

## 4. 专题合同（个人版要抄的）

- **多 Agent**：内部原生编排；并行只读 + 单写者；子 Agent 独立上下文。
- **定时**：overlap / catch-up / notify-on-change；接受 ≠ 完成 ≠ 送达。
- **RAG**：段落 chunk + BM25 两阶段 + RRF；先不要向量库。四时钟：event / published / available / revision。
- **Memory**：episodes / facts / procedures / blocks；ADD-only supersede；短记忆按 run 隔离。
- **Skills**：SKILL.md 渐进披露；技能不能自授工具权限。
- **回测**：trial ledger + 密封 holdout；DSR/PBO 抓不到泄漏。预测市场用 ForecastLedger + 相对市场中价的 Brier skill。
- **Trace**：OTel `gen_ai.*`；exactly-once 做不到；至少一次 + 稳定 `effect_id` + 对账 = 实际一次。
- **执行环境**：会话状态持久；计算可暂停；秘密不进 VM。

---

## 5. 目标架构（个人版相机）

确定性 FLOW 保留。research/analysis 扩成有界循环。交易不进图框架。SQLite + 现有租约。Codex CLI。FTS 不加向量库。Nautilus 先修会计。Langfuse + Prometheus。Compose 部署。

状态三分：调度器拥有 jobs/runs/effects；harness 拥有 checkpoint；OMS 拥有 orders/ledger。

数据血缘：`source → dataset → feature → hypothesis → experiment → forecast → proposal → order → fill → eval`。

---

## 6. 本轮源码落地（阶段 A）

| 合同 | 实现 |
|---|---|
| A1 执行 | `hub_effects` / `hub_execution_intents`；重试只读原 intent；SELL 不占新风险预算；可选 frozen 保护性退出 |
| A2 证据 | Proposal 绑 evidence hash / time_boundary；伪造/过期/越界阻断 |
| A3 研究循环 | `PlannerResponse` + 服务端 typed tools；`max_rounds`；干净 critic |
| A4/A5 账本 | trial_ledger；ForecastLedger / ResolutionLedger；`net_edge` 显式空因 |
| A6 调度 | overlap / catchup / notify_on_change / missed 原因 |
| A7 记忆/RAG | 段落 chunk + RRF；短记忆按 run 隔离；facts ADD-only |
| A8 可观测 | `gen_ai.*` 属性；分类评测；Prometheus 指标 |
| A9 UI | Workbench React：收件箱、运行三栏、调度、证据、Desk |

面试三条演示：有证据的 HOLD；UNKNOWN 对账只记一次；伪造证据程序拒绝。

---

## 7. 一手来源（本轮核验）

- AWS AgentCore Runtime V2：https://aws.amazon.com/about-aws/whats-new/2026/09/new-agentcore-runtime-generally-available/
- Heurist Finance：https://aws.amazon.com/blogs/machine-learning/how-heurist-finance-built-an-ai-native-investment-workbench-on-amazon-bedrock-agentcore/
- Microsoft Foundry September 2026：https://devblogs.microsoft.com/foundry/whats-new-in-microsoft-foundry-september-2026/
- Robinhood HOOD Summit：https://robinhood.com/us/en/newsroom/hood-summit-2026/
- QuantConnect Agents / Mia / Live Monitoring：https://www.quantconnect.com/docs/v2/ai-assistance/agents
- TradingAgents 0.7.0 changelog：https://tauricresearch.github.io/TradingAgents/CHANGELOG.html
- LangChain Deep Agents skills / context：https://www.langchain.com/blog/revamping-skills-in-deep-agents （2026-10-07）、https://www.langchain.com/blog/context-engineering-deep-agents-remember （2026-10-14）
- MCP 2026-07-28：https://modelcontextprotocol.io/specification/2026-07-28/changelog
