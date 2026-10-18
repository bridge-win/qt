# BTC 机会提醒验收记录 — 2026-10-18

实现与本机自动化验收已完成，实例保留在影子观察模式。
真实 Codex 鉴权、Telegram 实际收件以及策略晋级仍未通过，不能将这份记录解释为正式提醒已上线。

## 本机运行

- 地址：http://127.0.0.1:8877/btc-monitor ，使用已有本机登录。
- OrbStack 的 web、api、research-worker、monitor、alerts、reviewer 六个服务健康。
- Spot 与 perpetual 的已收盘 OHLCV 初始化成功；现货引用现有 WebSocket 的成交与报价新鲜度。
- 实盘执行关闭，提醒镜像不包含订单执行模块，不接收交易所账户密钥。
- 更新前 main 与 origin/main 一致，包含已发布基线 `eeab4c2707948dfbf6199ca6b03f87e03bdb1fa7`；快进 pull 没有冲突。
- 保留既有私有配置及三个数据卷，新增 alerts-state；没有切换全局 Docker context。

```bash
cd /Users/k/x/qt
bash deploy/orbstack.sh up
bash deploy/orbstack.sh status
bash deploy/orbstack.sh alerts-check
bash deploy/orbstack.sh alerts-mode shadow
bash deploy/orbstack.sh alerts-mode off
bash deploy/orbstack.sh down
```

`down` 保留数据卷。所有本机容器操作显式指定 OrbStack。
完整操作流程见 [机会提醒指南](../btc-alerts.md)。

## 已通过的自动化与运行检查

- 全量 Python 回归：1298 passed、8 skipped。此后新增资金费率上升的专项回归，相关完整测试集 306 passed。
- 八项跳过为七项未配置 PostgreSQL 的既有集成测试和一项需预构建插件镜像的既有测试。
- 前端 60 tests passed，TypeScript 检查及生产构建通过；依赖审计零漏洞。
- Ruff 全仓 src/tests 通过，新增提醒模块 Mypy 通过，Python wheel 构建通过。
- 新镜像 ARM64 与 AMD64 构建成功，实际 Codex CLI 版本 0.162.1。
- 匿名页面、监测 API、机会 API 均返回 401；已有本机登录访问机会 API 返回 200。
- 正常退出码 0，无 OOM；单独重启 alerts 后策略版本、配置版本与影子起点保留。
- 在线 SQLite 一致性备份可恢复；恢复后的策略、事件、发送箱、额度和研究验证记录一致。
- 隔离镜像验收证明：发送中断记为结果不明，重启不能绕过额度，镜像无交易执行包。
- 专项测试覆盖阈值边界、未收盘/乱序/缺口、结构化审核与无效引用、子进程超时清理、行情修订、最新成本、并发限流、时钟回拨、429、发送结果不明及鉴权。

本机 API 采用现有依赖与 requirements-orbstack.lock 全部 73 个包一致的运行层刷新源码；未变更依赖锁或读取运行中容器的凭据制作镜像。
CI 会从正式 Dockerfile 重新构建。此记录不预先声称 GitHub CI 或 Deploy TC 成功，应核对实现提交对应的运行结果。

## 已实际执行的研究

公开无鉴权 Binance Spot 15m 数据，730 天历史；固定工程阈值，最后 180 天为独立样本外窗口。
使用完整收盘数据、15 分钟虚拟入场延迟、成本、持有期去重和周分块 bootstrap；没有声称历史 Codex 预测能力。

| 策略方向 | 有效样本外事件 | 结果 |
| --- | ---: | --- |
| 趋势突破 / 买入 | 4 | 不足 100，未晋级 |
| 趋势突破 / 现货减仓 | 14 | 不足 100，未晋级 |
| 回撤恢复 / 买入 | 1 | 不足 100，未晋级 |
| 插针回收 / 买入 | 2 | 不足 100，未晋级 |
| 其他方向 | 0 | 未晋级 |
| 永续做空研究 | 0 | 历史资金费率覆盖不足，未晋级 |

660 个触发候选中，596 个未通过扣成本收益风险比，36 个因延迟入场价格变化无法入场；28 个完成虚拟后验计算，再剔除跨事件持有期重叠。
结果、公开数据哈希和基线对比保存在 alerts-state 的 research/；原始数据哈希为 `52998fc2ee5b909a8c46cb01935981b88a10be1da6b0b01b073fac1f9472d356`。
现金、持有 BTC、简单 EMA 趋势及没有 Codex 的数值规则均有比较记录。
这些结果不能证明盈利，也不满足前向七天和二十个完整后验事件的条件。

```bash
bash deploy/orbstack.sh alerts-research
```

历史研究使用独立临时容器，完成即移除，不占用提醒进程。

## 尚未通过与人工步骤

当前私有配置缺少以下字段，仅列名称：
`CODEX_API_KEY`、`QT_BTC_ALERTS_CODEX_MODEL`、`QT_TELEGRAM_BOT_TOKEN`、`QT_TELEGRAM_CHAT_ID`。
需要在本机终端运行隐藏输入流程，不把凭据发到聊天中：

```bash
bash deploy/orbstack.sh alerts-init
bash deploy/orbstack.sh up
```

Computer Use 的凭据变更规则要求由本人完成凭据生成/重置和输入提交。未复用此前泄露的 Token。
自动审批还阻止了打开显示名 radarxai 的 Telegram 私聊，因为该搜索结果尚未得到用户对 @radarxdotbot 身份对应关系的确认；没有绕过该阻止。
收到新配置和聊天确认后，应核验新鲜的 QT VERIFY 私聊挑战、执行真实模型 probe，再通过持久化发送箱发送 QT 测试并核对客户端唯一编号。
当前没有声称测试消息已发送或已收件。

浏览器 URL 安全策略拒绝观察本机页面，本轮没有绕过限制，因此浏览器视觉验收未完成；页面组件测试、构建和真实 HTTP API 鉴权检查已通过。
正式机会提醒受策略验证和前向观察硬门槛保护，不能为了验收手工填充生产通过记录。
本轮没有实际连续运行并检查满 24 小时，不构成 24 小时稳定性验收。
