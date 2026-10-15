import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Input, Space, Table, Tag, Typography } from "antd";
import { apiErrorText, request } from "../api";

const { Paragraph, Text } = Typography;
const TTL_SECONDS = 15;
const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

export type PolymarketOutcome = {
  name: string; token_id: string; bid: number | null; ask: number | null;
  midpoint: number | null; timestamp: number | null; ready: boolean; reasons: string[];
};
export type PolymarketSnapshot = {
  exchange: "polymarket"; environment: "production-public"; product: "prediction";
  slug: string; question: string; condition_id: string; version: "v1" | "v2";
  read_at: number; active: boolean; ready: boolean; reasons: string[];
  restricted?: boolean; outcomes: PolymarketOutcome[]; live_execution_enabled: false;
};
// Store receipt and transit time with each cached result, never with a mount.
export type ReceivedPolymarketSnapshot = {
  snapshot: PolymarketSnapshot; receivedMonotonicMs: number; requestDurationMs: number;
};
export const polymarketQueryKey = (slug: string | null) => ["polymarket-public", slug] as const;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}
function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
function nullableFinite(value: unknown): value is number | null {
  return value === null || finite(value);
}
function parseSnapshot(value: unknown, slug: string): PolymarketSnapshot {
  if (!record(value) || value.exchange !== "polymarket" || value.environment !== "production-public"
    || value.product !== "prediction" || value.slug !== slug || value.live_execution_enabled !== false
    || typeof value.question !== "string" || !value.question.trim() || typeof value.condition_id !== "string"
    || !value.condition_id.trim() || (value.version !== "v1" && value.version !== "v2")
    || !finite(value.read_at) || value.read_at <= 0 || !Number.isFinite(new Date(value.read_at * 1000).getTime())
    || typeof value.active !== "boolean" || typeof value.ready !== "boolean" || !strings(value.reasons)
    || (value.restricted !== undefined && typeof value.restricted !== "boolean")
    || !Array.isArray(value.outcomes) || !value.outcomes.every((outcome: unknown) => record(outcome)
      && typeof outcome.name === "string" && Boolean(outcome.name.trim())
      && typeof outcome.token_id === "string" && Boolean(outcome.token_id.trim())
      && nullableFinite(outcome.bid) && nullableFinite(outcome.ask) && nullableFinite(outcome.midpoint)
      && nullableFinite(outcome.timestamp) && typeof outcome.ready === "boolean" && strings(outcome.reasons))) {
    throw new Error("Invalid public prediction snapshot");
  }
  return value as PolymarketSnapshot;
}
function validBook(outcome: PolymarketOutcome, readAt: number): boolean {
  const { bid, ask, midpoint, timestamp } = outcome;
  return outcome.ready
    && bid !== null && ask !== null && midpoint !== null && timestamp !== null
    && bid >= 0 && ask <= 1 && bid <= ask && midpoint >= bid && midpoint <= ask
    && timestamp > 0 && timestamp <= readAt;
}
function oldestBookTimestamp(snapshot: PolymarketSnapshot): number {
  return snapshot.outcomes.reduce((oldest, outcome) => Math.min(oldest, outcome.timestamp ?? -Infinity), snapshot.read_at);
}
function probability(value: number | null): string {
  return value === null ? "不可用" : `${(value * 100).toFixed(2)}%`;
}
async function readSnapshot(slug: string, signal: AbortSignal): Promise<ReceivedPolymarketSnapshot> {
  const startedMonotonicMs = performance.now();
  const result = await request<unknown>(`/api/v3/prediction-markets/polymarket/${encodeURIComponent(slug)}`, { signal });
  const snapshot = parseSnapshot(result, slug);
  const receivedMonotonicMs = performance.now();
  return {
    snapshot, receivedMonotonicMs,
    requestDurationMs: Math.max(0, receivedMonotonicMs - startedMonotonicMs),
  };
}

export function PolymarketSnapshotCard(): ReactElement {
  const client = useQueryClient();
  const [input, setInput] = useState("");
  const [confirmedSlug, setConfirmedSlug] = useState<string | null>(null);
  const [inputError, setInputError] = useState(false);
  const [now, setNow] = useState(() => performance.now());
  const query = useQuery<ReceivedPolymarketSnapshot, Error>({
    queryKey: polymarketQueryKey(confirmedSlug),
    queryFn: ({ signal }) => readSnapshot(confirmedSlug!, signal),
    // Only the explicit submit handler may start a request, including remounts.
    enabled: false, retry: false, refetchOnMount: false,
    refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
  useEffect(() => {
    if (!query.data || !confirmedSlug) return;
    const { snapshot, receivedMonotonicMs, requestDurationMs } = query.data;
    const remainingMs = (oldestBookTimestamp(snapshot) + TTL_SECONDS - snapshot.read_at) * 1000
      - requestDurationMs - Math.max(0, performance.now() - receivedMonotonicMs);
    // Expire at the evidence deadline rather than waiting for another network
    // response or an arbitrary polling tick. This timer never makes a request.
    const id = window.setTimeout(() => setNow(performance.now()), Math.max(0, Math.ceil(remainingMs)));
    return () => window.clearTimeout(id);
  }, [query.data, confirmedSlug]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const slug = input.trim();
    if (slug.length > 200 || !SLUG_PATTERN.test(slug)) {
      setInputError(true);
      setConfirmedSlug(null);
      return;
    }
    setInputError(false);
    setConfirmedSlug(slug);
    setNow(performance.now());
    // A cached response cannot suppress an explicitly requested refresh.
    void client.fetchQuery({
      queryKey: polymarketQueryKey(slug), queryFn: ({ signal }) => readSnapshot(slug, signal),
      retry: false, staleTime: 0,
    }).catch(() => undefined); // The query observer presents the error, without retained quotes.
  }
  const received = confirmedSlug ? query.data : undefined;
  const snapshot = received?.snapshot;
  const elapsed = received ? (received.requestDurationMs + Math.max(0, now - received.receivedMonotonicMs)) / 1000 : 0;
  const oldest = snapshot ? oldestBookTimestamp(snapshot) : -Infinity;
  const expired = !snapshot || snapshot.read_at + elapsed >= oldest + TTL_SECONDS;
  const failed = query.isError || query.failureCount > 0;
  const current = Boolean(snapshot && !query.isFetching && !failed && !expired && snapshot.active
    && snapshot.ready && snapshot.outcomes.length > 0
    && snapshot.outcomes.every((outcome) => validBook(outcome, snapshot.read_at)));
  return <Card title="Polymarket 公共预测市场（只读）" className="top-gap">
    <Space wrap><Tag>Polymarket</Tag><Tag>production-public · 公共生产环境</Tag><Tag>prediction · 预测市场</Tag></Space>
    <Paragraph className="top-gap">手动输入市场 slug 并确认后，读取一次公共概率快照。概率数值仅为市场报价，不是预测或结果保证。无交易授权或执行功能。</Paragraph>
    <form onSubmit={submit}>
      <label htmlFor="polymarket-market-slug">Polymarket 市场 slug</label>
      <Space.Compact block className="top-gap">
        <Input id="polymarket-market-slug" value={input} placeholder="例如：will-event-happen" maxLength={200}
          aria-invalid={inputError} aria-describedby={inputError ? "polymarket-slug-error" : undefined}
          onChange={(event) => {
            setInput(event.target.value);
            setConfirmedSlug(null);
            setInputError(false);
          }} />
        <Button htmlType="submit" type="primary" loading={Boolean(confirmedSlug && query.isFetching)}>读取公共概率快照</Button>
      </Space.Compact>
    </form>
    {inputError && <Paragraph id="polymarket-slug-error" type="danger" role="alert">请输入 1–200 字符的小写字母、数字或连字符 slug；请勿输入网址。</Paragraph>}
    {!confirmedSlug && !inputError && <Paragraph type="secondary" className="top-gap">尚未确认市场，未发起公共行情查询。</Paragraph>}
    {confirmedSlug && query.isFetching && <Paragraph role="status" className="top-gap">正在读取公共概率快照，当前报价暂不显示。</Paragraph>}
    {confirmedSlug && failed && <Alert className="top-gap" type="error" showIcon message="概率快照读取失败，当前报价已隐藏" description={apiErrorText(query.error ?? query.failureReason)} />}
    {snapshot && <>
      <Descriptions className="top-gap" bordered size="small" column={{ xs: 1, md: 2 }}>
        <Descriptions.Item label="市场问题" span={2}>{snapshot.question}</Descriptions.Item>
        <Descriptions.Item label="市场 slug">{snapshot.slug}</Descriptions.Item>
        <Descriptions.Item label="协议版本">{snapshot.version}</Descriptions.Item>
        <Descriptions.Item label="条件 ID">{snapshot.condition_id}</Descriptions.Item>
        <Descriptions.Item label="读取时间（UTC）">{new Date(snapshot.read_at * 1000).toISOString()}</Descriptions.Item>
        <Descriptions.Item label="市场状态">{snapshot.active ? "活跃" : "非活跃"}</Descriptions.Item>
        <Descriptions.Item label="报价状态"><Tag color={current ? "blue" : "default"}>{current ? "公共概率报价可用" : "当前概率报价不可用"}</Tag></Descriptions.Item>
      </Descriptions>
      {snapshot.restricted && <Alert className="top-gap" type="warning" showIcon message="市场标记为受限" description="公开显示的市场报价不代表交易资格；请遵守适用的地域和市场限制。" />}
      {current && snapshot.reasons.length > 0 && <Paragraph type="secondary" className="top-gap">市场说明：{snapshot.reasons.join(" · ")}</Paragraph>}
      {!current && !query.isFetching && !failed && <Alert className="top-gap" type="warning" showIcon message={expired ? "公共概率快照已过期，请手动重新读取" : "公共盘口未就绪，当前概率报价不可用"} description={snapshot.reasons.join(" · ") || "盘口为空、单边、无效或市场未活跃时，不显示当前概率报价。"} />}
      <Table<PolymarketOutcome> className="top-gap" size="small" pagination={false} rowKey="token_id" dataSource={snapshot.outcomes} scroll={{ x: 650 }} locale={{ emptyText: "没有可用的结果盘口" }} columns={[
        { title: "结果", dataIndex: "name" },
        { title: "买方报价（概率）", render: (_, row) => current ? probability(row.bid) : "不可用" },
        { title: "卖方报价（概率）", render: (_, row) => current ? probability(row.ask) : "不可用" },
        { title: "中点（市场报价）", render: (_, row) => current ? probability(row.midpoint) : "不可用" },
        { title: "盘口说明", render: (_, row) => row.reasons.join(" · ") || (current ? "双边公共报价" : "未确认当前报价") },
      ]} />
      <Text type="secondary">报价有效期最多 15 秒，以最旧盘口时间与本地单调时钟计时；重新输入市场会清空显示，读取失败或过期后不会保留当前概率数值。</Text>
    </>}
  </Card>;
}
