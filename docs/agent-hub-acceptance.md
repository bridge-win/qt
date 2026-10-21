# Agent Hub 本机验收记录

本文是初版验收记录；本轮升级后的验证入口、范围和结果见 [Agent Hub v2](agent-hub-v2.md)。

验收日期：2026-10-21，Asia/Singapore。范围是个人单宿主机 Docker 版；研究和架构决策见 [架构报告](agent-hub-architecture.md)。本文只保存去敏结果，认证文件、模型原始输出与运行数据库不进入 Git。

## 已执行结果

| 项目 | 实际结果 |
| --- | --- |
| 镜像与启动 | `deploy/orbstack.sh hub-up` 成功；依赖按 hash 锁安装并通过 `pip check`；一次性 initialize 成功，8 个常驻服务 healthy |
| 容器测试 | `deploy/orbstack.sh hub-test`：169 passed，0 skipped；无外部网络、无账户凭据，镜像内真实安装的 Polymarket SDK 参与测试 |
| 既有功能回归 | BTC agent、交易门禁、预测市场、原 workbench 权限、研究队列/服务/仓库：159 passed |
| 静态检查 | 本次 Python 文件 Ruff lint/format、部署脚本 `bash -n`、前端 `node --check`、`git diff --check` 通过 |
| 运行时类型检查 | `mypy src/qt/agent_hub/runtime.py --strict --follow-imports=skip` 通过；没有宣称全仓库严格类型检查通过 |
| 原 workbench | `deploy/orbstack.sh check` 通过，原 API/worker 健康及未认证请求拒绝行为保持有效 |
| 本机 UI | `http://127.0.0.1:8878` 登录页已浏览器检查；登录后的功能经 API/运行时测试验证，没有声称完成全部浏览器点击验收 |
| 认证边界 | 未认证 API 返回 401；executor token 不能访问 owner API，owner token 不能调用模型执行器 |
| Codex 在线调用 | `hub-check` 实际使用本地登录的 OAuth 缓存调用容器内 Codex CLI，返回符合约束的结构化 HOLD；不是用 fixture 替代真实模型 |
| 完整主链 | research、analysis、backtest、forecast、risk_review、execute、log_review 均 succeeded；六个模型角色的持久化工件逐个核验 SHA-256 |
| 回测原始输入 | 999 根已收盘日线，保留完整 bars、规范请求、来源、抓取时间和独立输入工件；独立 sandbox 返回 completed |
| 交易判断 | 实际主链预测和风控均 HOLD；execute 为 held，原因 `agent_review_did_not_propose`，没有为了验收强制制造成交 |
| BTC 模拟成交 | 公开实时盘口驱动独立 paper 账本完成买入/卖出，重复请求返回同一订单，最终仅两单 |
| Polymarket 模拟成交 | 真实公开合约与订单簿驱动独立 paper 账本买入/卖出 2.500 shares，均 FILLED，最终持仓为零；幂等及跨 tenant 查询隔离通过 |
| 执行器局部重启 | 先确认共享 OAuth 调用锁被实际推理持有，再重启另一个 Codex 服务；其恢复 healthy，原调用及 smoke 完成 |
| 争用与恢复 | 六角色各连续遇到四次 busy 后仍完成，24 次 fenced defer 不消耗业务失败预算；真实失败仍有限次重试并可死信恢复 |
| UNKNOWN 故障回归 | 发送结果未知时不重发；monitor 查询原订单，确认后一次性记账；查询超时继续冻结，下轮确认恢复；重复监控不双记账 |

容器测试有一条 FastAPI/Starlette 关于 httpx TestClient 的弃用提示，没有测试失败。宿主机同类测试会跳过未安装的可选 Polymarket SDK；上表以安装完整锁定依赖的 Docker 测试为准。

## 可复核的运行证据

容器持久卷内保留 `/state/verification-report.json` 与 `/state/pipeline-verification-report.json`，前者由 `hub-check` 更新，后者记录本次主链的状态、工件哈希和回测输入引用。原始步骤工件保存在 `/files/personal/artifacts`，经 owner 认证后可在任务详情查看引用。运行文件不进入仓库。

本次完整主链的回测规范输入 hash 为 `4cc5eb3600aa37837136b3030f9677e7d411f7e98333fae8fffab50eef360d83`，完整输入工件 SHA-256 为 `c2a8979ac74d4b567154de4027cd1cf410a72ab2b1fc6cb3955eacfb15d13689`。日志复盘工件 SHA-256 为 `7147187910991ac61327807d4c20e4436e5142a09e1e76c06cf75d76d6b56ba3`。这些值用于核对文件，不代表模型结论正确或策略盈利。

Polymarket 模拟验收的公开订单簿当时 bid=0.36、ask=0.37，token SHA-256 前缀 `ec5ce1f70883`；纸面费用合计 0.0811125 pUSD，含价差后的账本损益为 −0.1061125 pUSD。费用采用项目明确的近似模型；这不是实际账户成交或交易所费用核对。公开市场随时间变化，后续 smoke 会在最多 3 个市场、每市场 2 个 token 中寻找就绪订单簿；没有就绪数据时记录失败原因，不能用模拟数据替代公开数据成功。

复核入口：

```bash
./deploy/orbstack.sh hub-status
./deploy/orbstack.sh hub-test
./deploy/orbstack.sh hub-check
./deploy/orbstack.sh hub-exec api python -c 'from pathlib import Path; print(Path("/state/pipeline-verification-report.json").read_text())'
```

`hub-check` 会发出一次真实 Codex 调用，并在独立验证账本使用模拟资金成交；它不选择生产执行模式。定时主链由 YAML 管理，实际采集与自动执行开关应以部署配置为准。

## 验收边界

本次没有发送真实资金订单，也没有完成 Binance authenticated 测试网/实盘或 Polymarket 钱包成交验收。交易凭据文件初始为空；地区限制仍有效，SG close-only 不通过更换服务器位置绕过。

本次没有部署阿里云/腾讯云账户资源，没有实施完整多租户身份与配额、微虚机任意代码执行、语义向量检索、生产告警平台、备份恢复演练或长期稳定性/收益/概率校准证明。架构报告明确列出这些后续能力及个人版的可迁移边界。当前有服务器固定 tenant 的数据隔离基础、固定函数 Docker sandbox、FTS/BM25 检索和有限 MCP 工具入口。
