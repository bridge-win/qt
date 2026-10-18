import { useEffect, useState, type ReactElement } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Drawer, InputNumber, Space, Table, Tag, Typography } from "antd";
import { apiErrorText, post, request } from "../api";

type Mode = "off" | "shadow" | "notify";
type Policy = Record<string, unknown> & { mode: Mode; thresholds: Record<string, number> };
type Event = {
  event_id: string; created_at: number; status: string; reason: string;
  payload: { direction?: string; reference_price?: number; net_reward_risk?: number; is_test?: boolean };
};
type CardSource = { evidence_id: string; title: string; url: string };
export type AlertsProjection = {
  configured: boolean; server_time: number; revision: number; strategy_version: string; policy: Policy;
  readiness: { software: boolean; model: boolean; recipient: boolean; strategies: boolean };
  worker: {
    alive: boolean; status: string; recipient_verified?: boolean; notifications_configured?: boolean;
    next_allowed_at?: number; reviewer?: { authenticated: boolean; model?: string; model_pinned?: boolean };
    markets?: Record<string, { ready: boolean; reason?: string; bar_close?: number }>;
  };
  cards: { strategy_id: string; name: string; counterexample: string; rule_origin: string; sources: CardSource[] }[];
  validations: { strategy_id: string; direction: string; status: string; oos_events: number; ci95_lower?: number | null }[];
  events: Event[];
};
type Received = { value: AlertsProjection; receivedAt: number };
const labels: Record<string, string> = {
  paused: "已暂停", watching: "观察中", data_unavailable: "行情尚未就绪", worker_unavailable: "提醒服务离线",
  queued: "待审核", reviewing: "Codex 审核中", review_rejected: "审核未通过",
  review_unavailable: "审核不可用", review_unknown: "审核中断", budget_blocked: "审核预算限制",
  evidence_pending: "验证证据不足", shadow_approved: "影子审核通过", approved: "待发送",
  cooldown: "一小时冷却中", accepted: "Telegram 已接受", unknown: "发送结果不明",
  failed: "发送失败", expired: "已过期", obsolete: "发送前校验未通过", test_queued: "测试通知排队中",
  long: "买入研究", short: "做空研究", reduce_long: "现货减仓研究",
};
const thresholdFields: [string, string, number, number, number][] = [
  ["breakout_volume", "突破成交量倍数", 1, 10, 0.1],
  ["distress_rsi", "极端回撤 RSI 上限", 5, 40, 1],
  ["distress_volume", "回撤成交量倍数", 1, 10, 0.1],
  ["wick_body", "影线／实体下限", 1, 10, 0.1],
  ["wick_fraction", "影线／振幅下限", 0.5, 0.99, 0.01],
  ["wick_atr", "插针振幅／ATR 下限", 1, 10, 0.1],
  ["overheat_rsi", "过热 RSI 下限", 65, 95, 1],
  ["overheat_recovery_rsi", "转弱 RSI 上限", 50, 85, 1],
];
function stamp(value: number | undefined): string {
  return value && Number.isFinite(value) ? new Date(value * 1000).toISOString() : "—";
}
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : apiErrorText(error);
}

export function BtcAlertsCard(): ReactElement {
  const client = useQueryClient();
  const [stale, setStale] = useState(false);
  const [selected, setSelected] = useState<string>();
  const [draft, setDraft] = useState<Record<string, number> | null>(null);
  const query = useQuery<Received>({
    queryKey: ["btc-alerts", "/api/v3/btc-alerts"],
    queryFn: async () => ({ value: await request<AlertsProjection>("/api/v3/btc-alerts"), receivedAt: performance.now() }),
    refetchInterval: 5000, retry: false,
  });
  const receivedAt = query.data?.receivedAt;
  useEffect(() => {
    setStale(false);
    if (receivedAt === undefined) return;
    const timer = window.setTimeout(() => setStale(true), Math.max(0, 20_000 - (performance.now() - receivedAt)));
    return () => window.clearTimeout(timer);
  }, [receivedAt]);
  const data = query.data?.value;
  const fresh = !!query.data && !query.isError && !stale && performance.now() - query.data.receivedAt <= 20_000;
  const alive = fresh && data?.worker?.alive;
  const now = (data?.server_time ?? 0) + (query.data ? (performance.now() - query.data.receivedAt) / 1000 : 0);
  const detail = useQuery({
    queryKey: ["btc-alert-event", selected],
    queryFn: () => request<Record<string, unknown>>("/api/v3/btc-alerts/events/" + encodeURIComponent(selected!)),
    enabled: !!selected, retry: false,
  });
  const save = useMutation({
    mutationFn: (policy: Policy) => request<AlertsProjection>("/api/v3/btc-alerts/policy", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: data!.revision, policy }),
    }),
    onSuccess: () => { setDraft(null); void client.invalidateQueries({ queryKey: ["btc-alerts"] }); },
  });
  const test = useMutation({
    mutationFn: () => post<{ event_id: string }>("/api/v3/btc-alerts/test-notification", { request_id: crypto.randomUUID() }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["btc-alerts"] }); },
  });
  const canNotify = !!(alive && data?.readiness?.model && data.readiness.recipient
    && data.readiness.strategies && data.worker.reviewer?.model_pinned);
  return <Card title="BTC 机会提醒" className="top-gap">
    <Typography.Paragraph>15 分钟收盘触发，1 小时／4 小时过滤。买入、减仓和做空研究合计，任意连续 60 分钟最多一条；没有合格机会时保持安静。</Typography.Paragraph>
    {query.isLoading && <Typography.Text>正在读取提醒状态…</Typography.Text>}
    {query.isError && <Alert type="warning" showIcon message="提醒状态读取失败" description={apiErrorText(query.error)} />}
    {data && !data.configured && <Alert type="info" showIcon message="此主机尚未启动机会提醒服务" description="本机 OrbStack 提醒实例的状态应在本机页面查看。" />}
    {data?.configured && <>
      <Space wrap className="bottom-gap">
        <Tag color={alive ? "green" : "orange"}>{alive ? labels[data.worker.status] ?? data.worker.status : "提醒服务状态不可确认"}</Tag>
        <Tag>当前模式：{{ off: "暂停", shadow: "影子观察", notify: "正式提醒" }[data.policy.mode]}</Tag>
        <Tag>自动交易关闭</Tag>
      </Space>
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="策略版本">{data.strategy_version}</Descriptions.Item>
        <Descriptions.Item label="配置版本">{data.revision}</Descriptions.Item>
        <Descriptions.Item label="Codex">{data.worker.reviewer?.authenticated ? "已通过实际调用" : "尚未通过实际调用"} · {data.worker.reviewer?.model ?? "模型未核验"}</Descriptions.Item>
        <Descriptions.Item label="Telegram">{data.worker.recipient_verified ? "私聊接收人已核验" : data.worker.notifications_configured ? "已配置，接收人待核验" : "未配置"}</Descriptions.Item>
        <Descriptions.Item label="行情">{Object.entries(alive ? data.worker.markets ?? {} : {}).map(([key, value]) => key + "：" + (value.ready ? "已就绪" : value.reason ?? "数据不足")).join(" · ") || "状态不可确认"}</Descriptions.Item>
        <Descriptions.Item label="下次允许发送">{!alive ? "状态不可确认" : (data.worker.next_allowed_at ?? 0) > now ? stamp(data.worker.next_allowed_at) : "额度可用"}</Descriptions.Item>
      </Descriptions>
      <Space wrap className="top-gap">
        <Button disabled={!alive || save.isPending} onClick={() => save.mutate({ ...data.policy, mode: "off" })}>暂停提醒</Button>
        <Button disabled={!alive || save.isPending} onClick={() => save.mutate({ ...data.policy, mode: "shadow" })}>影子观察</Button>
        <Button type="primary" disabled={!canNotify || save.isPending} onClick={() => save.mutate({ ...data.policy, mode: "notify" })}>启用正式提醒</Button>
        <Button disabled={!alive || !data.worker.recipient_verified || data.policy.mode === "off" || test.isPending} onClick={() => test.mutate()}>发送 QT 测试</Button>
      </Space>
      {!canNotify && <Alert className="top-gap" type="info" showIcon message="正式机会提醒尚未满足全部条件" description="需要已核验的模型和接收聊天、样本外策略结果及前向影子观察证据。高证据等级不等于胜率。" />}
      {save.isError && <Alert className="top-gap" type="warning" message="配置未保存" description={errorMessage(save.error)} />}
      {test.isError && <Alert className="top-gap" type="warning" message="测试未排队" description={errorMessage(test.error)} />}
      {test.isSuccess && <Alert className="top-gap" type="success" message="QT 测试已排队，实际收件需在 Telegram 核对" description={test.data.event_id} />}
      <Card size="small" title="阈值配置" className="top-gap">
        <Space wrap align="start">
          {thresholdFields.map(([key, label, min, max, step]) => <Space direction="vertical" key={key}>
            <Typography.Text>{label}</Typography.Text>
            <InputNumber aria-label={label} min={min} max={max} step={step} value={(draft ?? data.policy.thresholds)[key]} onChange={(value) => {
              if (typeof value === "number") setDraft({ ...(draft ?? data.policy.thresholds), [key]: value });
            }} />
          </Space>)}
        </Space>
        <Typography.Paragraph type="secondary" className="top-gap">参数更改会生成新策略版本，并进入影子观察。模型不能改写这些阈值。</Typography.Paragraph>
        <Button disabled={!draft || save.isPending} onClick={() => save.mutate({ ...data.policy, thresholds: draft!, mode: "shadow" })}>保存为新策略版本</Button>
      </Card>
      <Table className="top-gap" size="small" rowKey="event_id" pagination={{ pageSize: 10 }} scroll={{ x: 850 }} dataSource={data.events} columns={[
        { title: "事件", render: (_, row) => <Button type="link" onClick={() => setSelected(row.event_id)}>{row.event_id.slice(0, 16)}</Button> },
        { title: "方向", render: (_, row) => row.payload.is_test ? "QT 测试" : labels[row.payload.direction ?? ""] ?? "—" },
        { title: "状态", render: (_, row) => labels[row.status] ?? row.status },
        { title: "原因", dataIndex: "reason" }, { title: "时间", render: (_, row) => stamp(row.created_at) },
      ]} locale={{ emptyText: "暂无触发记录；服务不会为了发送频率制造机会。" }} />
      <Card size="small" title="策略资料与验证" className="top-gap">
        {data.cards.map((card) => <div key={card.strategy_id} className="top-gap">
          <Typography.Text strong>{card.name}</Typography.Text>
          <Typography.Paragraph>{card.counterexample}</Typography.Paragraph>
          <Space wrap>{card.sources.map((source) => <a key={source.evidence_id} href={source.url} target="_blank" rel="noreferrer">{source.title}</a>)}</Space>
        </div>)}
        <Table className="top-gap" size="small" rowKey={(row) => row.strategy_id + ":" + row.direction} pagination={false} dataSource={data.validations} columns={[
          { title: "策略", dataIndex: "strategy_id" }, { title: "方向", render: (_, row) => labels[row.direction] },
          { title: "样本外事件", dataIndex: "oos_events" }, { title: "验证", render: (_, row) => row.status === "passed" ? "数值验证通过，仍需前向证据" : "证据不足" },
        ]} />
      </Card>
    </>}
    <Drawer title="机会触发、审核与发送依据" open={!!selected} onClose={() => setSelected(undefined)} width={760}>
      {detail.isLoading && <Typography.Text>正在读取事件…</Typography.Text>}
      {detail.isError && <Alert type="warning" message={apiErrorText(detail.error)} />}
      {detail.data && <pre className="response-preview">{JSON.stringify(detail.data, null, 2)}</pre>}
    </Drawer>
  </Card>;
}
