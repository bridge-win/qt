import { useEffect, useState, type ReactElement } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Descriptions, Empty, Space, Table, Tag, Typography } from "antd";
import { apiErrorText, request } from "../api";
import { PageTitle } from "../workbench";
import { BtcAlertsCard } from "./BtcAlertsCard";
import { PolymarketSnapshotCard } from "./PolymarketSnapshotCard";

const { Paragraph, Text } = Typography;

type Channel = {
  last_event_at: number | null;
  last_receive_at: number | null;
  event_age_seconds: number | null;
  receive_age_seconds: number | null;
  backlog_seconds: number | null;
  ready: boolean;
  source_time_known: boolean;
};
type Gate = { name: string; passed: boolean; value: unknown; threshold: unknown; reason: string };
type Decision = {
  assessment_id: string; revision: number; as_of: number; expires_at: number;
  state: "INSUFFICIENT_DATA" | "DATA_UNHEALTHY" | "UNSUITABLE" | "CANDIDATE";
  reasons: string[]; gates: Gate[]; evidence: Record<string, unknown>;
  range_lower: number | null; range_upper: number | null; spacing_bps: number | null;
  net_spacing_bps: number | null; levels: number | null; missing_inputs: string[];
};
type Anomaly = {
  event_id: string; revision: number; state: "WATCH" | "CONFIRMED" | "INVALIDATED" | "EXPIRED";
  as_of: number; expires_at: number; direction: string; baseline: number; extreme: number;
  recovery_fraction: number; reason: string;
};
export type BtcMonitorProjection = {
  read_status: string; read_reasons: string[]; server_time: number; valid_until: number | null; data_valid_until?: number | null;
  snapshot_age_seconds: number | null; snapshot_max_age_seconds: number;
  snapshot: null | {
    schema_version: number; config_version: string; boot_id: string; published_at: number;
    source: { exchange: string; environment?: "production" | "testnet" | "demo"; product: string; symbol: string; display_symbol: string; price_type: string };
    health: {
      alive: boolean; data_ready: boolean; assessment_ready: boolean; status: string; reasons: string[];
      channels: Record<string, Channel>; coverage_seconds: number; errors: Record<string, number>;
    };
    micro: { price?: number; spread_bps?: number }; assessment: Decision; recent_events: Anomaly[];
    context: { notifications_configured?: boolean; notification_status?: { configured?: boolean };
      sensitivity_preset?: string; tick_size_assumed?: boolean; tick_size?: number; thresholds_calibrated?: boolean };
  };
};

// Receipt time belongs to the cached response, so remounts and failed refetches
// cannot renew its lifetime. It is local-only and never sent to the API.
export type ReceivedBtcMonitorProjection = { projection: BtcMonitorProjection; receivedMonotonicMs: number; requestDurationMs: number };

const states: Record<Decision["state"], string> = {
  CANDIDATE: "市场条件候选（仅研究）", INSUFFICIENT_DATA: "数据不足", DATA_UNHEALTHY: "数据不可用", UNSUITABLE: "当前不适合网格",
};
const eventStates: Record<Anomaly["state"], string> = { WATCH: "观察", CONFIRMED: "已确认", INVALIDATED: "已撤销", EXPIRED: "已过期" };
const inputLabels: Record<string, string> = {
  budget: "可用预算", saleable_btc_inventory: "可卖出 BTC 库存", actual_fee_tier: "账户实际费率",
  exchange_order_size_constraints: "交易所下单数量约束", cross_venue_confirmation_unavailable: "跨平台确认",
  strategy_profitability_unvalidated: "策略盈利验证",
};
function number(value: unknown, suffix = ""): string { return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}${suffix}` : "未提供"; }
function stamp(value: number): string {
  if (!Number.isFinite(value)) return "无效时间";
  const date = new Date(value * 1000);
  return Number.isFinite(date.getTime()) ? date.toISOString() : "无效时间";
}
function scalar(value: unknown): string {
  if (typeof value === "number") return number(value);
  if (value === null || value === undefined) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function BtcMonitorPage(): ReactElement {
  const query = useQuery<ReceivedBtcMonitorProjection, Error>({
    queryKey: ["btc-monitor", "/api/v3/btc-monitor"],
    queryFn: async ({ signal }) => {
      const startedMonotonicMs = performance.now();
      const projection = await request<BtcMonitorProjection>("/api/v3/btc-monitor", { signal });
      const receivedMonotonicMs = performance.now();
      return { projection, receivedMonotonicMs, requestDurationMs: Math.max(0, receivedMonotonicMs - startedMonotonicMs) };
    },
    refetchInterval: 2_000,
  });
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => { const id = window.setInterval(() => setNow(performance.now()), 250); return () => window.clearInterval(id); }, []);
  const pollingFailed = query.isError || query.failureCount > 0;
  const data = query.data?.projection;
  const snapshot = data?.snapshot;
  // Advance the server clock using monotonic elapsed time. A client wall-clock
  // rollback, retained cache, or unresolved/erroring fetch cannot freeze the TTL.
  // Charge the full request duration against freshness, conservatively including
  // response transit without relying on synchronized server/client wall clocks.
  const elapsed = ((query.data?.requestDurationMs ?? 0) + Math.max(0, now - (query.data?.receivedMonotonicMs ?? now))) / 1000;
  const serverNow = (data?.server_time ?? 0) + elapsed;
  const expired = !data?.valid_until || serverNow >= data.valid_until;
  const sourceCurrent = Boolean(snapshot && !pollingFailed && snapshot.health.data_ready && serverNow < (data?.data_valid_until ?? snapshot.published_at + (data?.snapshot_max_age_seconds ?? 10)));
  const processCurrent = Boolean(snapshot && !pollingFailed && snapshot.health.alive && serverNow < snapshot.published_at + (data?.snapshot_max_age_seconds ?? 10));
  const current = Boolean(snapshot && !pollingFailed && data?.read_status === "ok" && !expired && snapshot.health.data_ready && snapshot.health.assessment_ready);
  const candidate = current && snapshot?.assessment.state === "CANDIDATE";
  const decision = snapshot?.assessment;
  const channelRows = Object.entries(snapshot?.health.channels ?? {}).map(([name, channel]) => ({ name, ...channel }));
  const notificationConfigured = snapshot?.context.notifications_configured ?? snapshot?.context.notification_status?.configured ?? false;
  const venue = snapshot?.source.exchange === "binance" ? "Binance" : snapshot?.source.exchange === "okx" ? "OKX" : snapshot?.source.exchange;
  // Snapshots written before environment identity was introduced are production.
  const environment = snapshot?.source.environment ?? "production";
  const environmentLabel = environment === "demo" ? "模拟盘 DEMO" : environment === "testnet" ? "测试网 TESTNET" : "生产环境 production";
  return <>
    <PageTitle eyebrow={`${venue ? `${venue} · ${environmentLabel}` : "BTC"} 现货 · 公共行情 · 只读`} title="BTC 实时监测">
      <Paragraph>观察秒级插针与网格市场适用性。仅用于监测与研究；不提交实盘订单，不计算下单数量，不授权交易。</Paragraph>
    </PageTitle>
    {snapshot && environment !== "production" && <Alert className="research-notice" type="warning" showIcon message={`${venue} ${environmentLabel} · 非生产行情`} description="当前 BTC 现货监测来自测试或模拟环境；价格与评估仅适用于所标示环境。" />}
    {pollingFailed && <Alert type="error" showIcon message="监测读取失败，候选已隐藏" description={apiErrorText(query.error ?? query.failureReason)} />}
    {query.isLoading && <Card loading aria-label="正在读取监测状态" />}
    {!query.isLoading && !snapshot && <Card><Empty description="监测尚未启动或快照不可用" /><Text type="secondary">{data?.read_reasons.join(" · ")}</Text></Card>}
    {snapshot && <>
      {!current && <Alert className="research-notice" type="warning" showIcon message="当前评估不可用" description={pollingFailed ? "本次轮询失败。保留的数据仅作历史诊断。" : expired ? "快照或评估已过期。等待新的有效证据。" : [...(data?.read_reasons ?? []), ...snapshot.health.reasons].join(" · ")} />}
      <Card title="行情身份与健康" className="top-gap">
        <Descriptions bordered size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="市场">{snapshot.source.exchange} · {snapshot.source.product} · {snapshot.source.display_symbol} ({snapshot.source.symbol})</Descriptions.Item>
          <Descriptions.Item label="行情环境"><Tag color={environment === "production" ? "blue" : "orange"}>{environmentLabel}</Tag></Descriptions.Item>
          <Descriptions.Item label="价格类型">{snapshot.source.price_type}（现货成交价）</Descriptions.Item>
          <Descriptions.Item label="最新价格">{number(snapshot.micro.price, " USDT")}</Descriptions.Item>
          <Descriptions.Item label="买卖价差">{number(snapshot.micro.spread_bps, " bps")}</Descriptions.Item>
          <Descriptions.Item label="发布时间">{stamp(snapshot.published_at)}</Descriptions.Item>
          <Descriptions.Item label="快照年龄">{number((data?.snapshot_age_seconds ?? 0) + elapsed, " 秒")}</Descriptions.Item>
          <Descriptions.Item label="进程存活"><Tag color={processCurrent ? "blue" : "default"}>{processCurrent ? "有近期进展" : "未确认"}</Tag></Descriptions.Item>
          <Descriptions.Item label="数据就绪">{sourceCurrent ? "是" : "否"} · 评估就绪：{current ? "是" : "否"}</Descriptions.Item>
          <Descriptions.Item label="路径覆盖">{number(snapshot.health.coverage_seconds, " 秒")}</Descriptions.Item>
          <Descriptions.Item label="通知">{notificationConfigured ? "已配置（不代表已送达设备）" : "未配置"}</Descriptions.Item>
          <Descriptions.Item label="会话">{snapshot.boot_id}</Descriptions.Item>
          <Descriptions.Item label="配置版本">{snapshot.config_version}</Descriptions.Item>
          <Descriptions.Item label="灵敏度预设">{snapshot.context.sensitivity_preset === "balanced" ? "balanced（均衡启发式，未实证校准）" : snapshot.context.sensitivity_preset ?? "未提供"}</Descriptions.Item>
          <Descriptions.Item label="价格步长假设">{snapshot.context.tick_size_assumed ? `${number(snapshot.context.tick_size, " USDT")}（假设，交易所约束未核验）` : "未提供已核验步长"}</Descriptions.Item>
        </Descriptions>
        <Table className="top-gap" size="small" pagination={false} rowKey="name" dataSource={channelRows} scroll={{ x: 700 }} columns={[
          { title: "通道", dataIndex: "name" },
          { title: "新鲜度", render: (_, row) => sourceCurrent && row.ready ? "就绪" : "未就绪" },
          { title: "源事件年龄", render: (_, row) => row.source_time_known ? number(row.event_age_seconds === null ? null : row.event_age_seconds + elapsed, " 秒") : "源无时间戳（使用接收时间）" },
          { title: "接收年龄", render: (_, row) => number(row.receive_age_seconds === null ? null : row.receive_age_seconds + elapsed, " 秒") },
          { title: "消费延迟", render: (_, row) => number(row.backlog_seconds, " 秒") },
        ]} />
        <Paragraph type="secondary" className="top-gap">健康 / 缺口原因：{snapshot.health.reasons.join(" · ") || "无已报告缺口"}；错误计数：{Object.entries(snapshot.health.errors).map(([name, count]) => `${name}: ${count}`).join(" · ") || "0"}</Paragraph>
      </Card>
      {decision && <Card className="top-gap" title="网格适用性与成本依据">
        <Space wrap><Tag color={candidate ? "green" : "default"}>{candidate ? states.CANDIDATE : current ? states[decision.state] : "评估暂停 / 历史证据"}</Tag><Text>{decision.assessment_id} · r{decision.revision}</Text></Space>
        <Paragraph className="top-gap">{decision.reasons.join(" · ")}</Paragraph>
        <Descriptions bordered size="small" column={{ xs: 1, md: 2 }}>
          <Descriptions.Item label="证据截至">{stamp(decision.as_of)}</Descriptions.Item>
          <Descriptions.Item label="评估到期">{stamp(decision.expires_at)}</Descriptions.Item>
          <Descriptions.Item label="研究区间">{number(decision.range_lower)} ~ {number(decision.range_upper)} USDT</Descriptions.Item>
          <Descriptions.Item label="研究层数">{decision.levels ?? "未提供"}</Descriptions.Item>
          <Descriptions.Item label="间距 / 扣成本后净间距">{number(decision.spacing_bps, " bps")} / {number(decision.net_spacing_bps, " bps")}</Descriptions.Item>
          <Descriptions.Item label="成本假设">{costSummary(decision.evidence)}</Descriptions.Item>
          <Descriptions.Item label="缺少账户与验证输入" span={2}>{decision.missing_inputs.map((value) => inputLabels[value] ?? value).join(" · ") || "未报告"}</Descriptions.Item>
        </Descriptions>
        <Paragraph type="secondary">区间与净间距是启发式市场研究，不是盈利概率或可执行方案。预算、库存与账户条件未经确认。</Paragraph>
        <Table size="small" rowKey="name" pagination={false} dataSource={decision.gates} scroll={{ x: 700 }} columns={[
          { title: "门槛", dataIndex: "name" }, { title: "证据结果", render: (_, row) => row.passed ? "通过（记录时）" : "未通过" },
          { title: "测量值", render: (_, row) => scalar(row.value) }, { title: "阈值", render: (_, row) => scalar(row.threshold) }, { title: "原因", dataIndex: "reason" },
        ]} />
      </Card>}
      <Card title="插针事件生命周期" className="top-gap">
        <Table size="small" rowKey={(row) => `${row.event_id}:${row.revision}`} pagination={false} dataSource={snapshot.recent_events} scroll={{ x: 800 }} locale={{ emptyText: "暂无已记录插针事件" }} columns={[
          { title: "事件 / 版本", render: (_, row) => `${row.event_id} · r${row.revision}` },
          { title: "状态", render: (_, row) => eventStates[(!sourceCurrent || pollingFailed || serverNow >= row.expires_at || serverNow > snapshot.published_at + data!.snapshot_max_age_seconds) && (row.state === "WATCH" || row.state === "CONFIRMED") ? "EXPIRED" : row.state] },
          { title: "方向", render: (_, row) => row.direction === "down" ? "向下" : "向上" },
          { title: "基准 / 极值", render: (_, row) => `${number(row.baseline)} / ${number(row.extreme)}` },
          { title: "恢复比例", render: (_, row) => number(row.recovery_fraction * 100, "%") },
          { title: "时间", render: (_, row) => stamp(row.as_of) }, { title: "依据", dataIndex: "reason" },
        ]} />
      </Card>
    </>}
    <BtcAlertsCard />
    <PolymarketSnapshotCard />
  </>;
}

function costSummary(evidence: Record<string, unknown>): string {
  const keys = ["fee_per_side_bps", "slippage_per_side_bps", "cost_buffer_bps", "round_trip_cost_bps", "cost_bps"];
  const labels: Record<string, string> = { fee_per_side_bps: "单边手续费", slippage_per_side_bps: "单边滑点", cost_buffer_bps: "缓冲", round_trip_cost_bps: "往返成本", cost_bps: "总成本" };
  const values = keys.filter((key) => typeof evidence[key] === "number").map((key) => `${labels[key]} ${number(evidence[key], " bps")}`);
  return values.join(" · ") || "见成本门槛；未提供账户实际费率";
}
