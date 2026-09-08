import { lazy, Suspense, useState, type ReactElement } from "react";
import { useMutation } from "@tanstack/react-query";
import { Alert, Card, Descriptions, Select, Space, Table, Tabs, Typography } from "antd";
import { apiErrorText, post } from "../api";
import { PriceChart, type PricePoint } from "../charts/PriceChart";
import { ApiAction, QueryState } from "../components";
import { PageTitle, type UnknownRecord, useApi } from "../workbench";

const { Paragraph, Text } = Typography;
const AnalysisChart = lazy(() => import("../charts/AnalysisChart"));

export function ComparePage(): ReactElement {
  const results = useApi<{ items: UnknownRecord[] }>("results", "/api/v3/results?limit=100");
  const [ids, setIds] = useState<string[]>([]);
  const compare = useMutation({ mutationFn: () => post<UnknownRecord>("/api/v3/compare", { result_ids: ids }) });

  return (
    <>
      <PageTitle eyebrow="同条件比较" title="比较策略、参数和组合">
        <Paragraph>从已发布结果选择至少两项。后端检查数据、成本、风险预算和执行模型是否可比，并返回正式指标与下采样序列。</Paragraph>
      </PageTitle>
      <Card>
        <QueryState loading={results.isLoading} error={results.error}>
          <Select
            mode="multiple"
            className="full-width"
            aria-label="选择比较结果"
            value={ids}
            onChange={setIds}
            options={(results.data?.items ?? []).map((item) => ({
              value: String(item.result_id ?? item.run_id ?? item.id),
              label: `${String(item.result_id ?? item.run_id ?? item.id)} · ${String(item.status ?? "已发布")}`,
            }))}
            placeholder="选择两个或以上已发布结果"
          />
        </QueryState>
        <Space className="page-actions">
          <ApiAction label="请求服务端比较" loading={compare.isPending} disabled={ids.length < 2} onClick={() => compare.mutate()} />
        </Space>
        {compare.isError && <Alert type="error" showIcon message="比较不可用" description={apiErrorText(compare.error)} />}
        {compare.data && <ComparisonResult result={compare.data} />}
      </Card>
    </>
  );
}

function ComparisonResult({ result }: { result: UnknownRecord }): ReactElement {
  const seriesByResult = result.series && typeof result.series === "object" ? result.series as UnknownRecord : {};
  const resultIds = Object.keys(seriesByResult);
  const [selectedResultId, setSelectedResultId] = useState("");
  const activeResultId = selectedResultId || resultIds[0] || "";
  const activeSeries = seriesByResult[activeResultId];
  const activeValues = activeSeries && typeof activeSeries === "object" ? activeSeries as UnknownRecord : {};
  const costs = result.costs && typeof result.costs === "object" ? (result.costs as UnknownRecord)[activeResultId] : undefined;
  const monthly = result.monthly_returns && typeof result.monthly_returns === "object" ? (result.monthly_returns as UnknownRecord)[activeResultId] : undefined;
  const metricRows = Array.isArray(result.metrics) ? result.metrics.flatMap((entry) => metricRowsFor(entry)) : [];

  return (
    <div className="top-gap">
      <Select aria-label="选择比较结果图表" className="full-width" value={activeResultId || undefined} onChange={setSelectedResultId} options={resultIds.map((id) => ({ value: id, label: id }))} placeholder="服务端没有返回可比时间序列" />
      <Tabs items={[
        {
          key: "metrics",
          label: "正式指标",
          children: <>
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="可比结果">{Array.isArray(result.comparable_result_ids) ? result.comparable_result_ids.join(", ") : "服务未提供"}</Descriptions.Item>
              <Descriptions.Item label="不可比结果">{incompatibleText(result.incompatible)}</Descriptions.Item>
            </Descriptions>
            <Table rowKey="key" pagination={false} dataSource={metricRows} columns={[{ title: "结果", dataIndex: "result_id" }, { title: "指标", dataIndex: "metric" }, { title: "值", dataIndex: "value" }]} />
          </>,
        },
        { key: "equity", label: "净值", children: <PriceChart points={toPoints(activeValues.equity)} /> },
        { key: "drawdown", label: "回撤", children: <PriceChart points={toPoints(activeValues.drawdown)} /> },
        { key: "costs", label: "成本", children: <pre className="response-preview">{JSON.stringify(costs ?? "服务端未提供该结果成本", null, 2)}</pre> },
        { key: "monthly", label: "月度收益", children: <pre className="response-preview">{JSON.stringify(monthly ?? "服务端未提供该结果月度收益", null, 2)}</pre> },
        { key: "analysis", label: "参数与风险", children: <Suspense fallback={<Text>正在加载分析图表…</Text>}><AnalysisChart title="服务端比较分析" points={toAnalysisPoints(result.analysis_series)} /></Suspense> },
      ]} />
    </div>
  );
}

function metricRowsFor(entry: unknown): Array<{ key: string; result_id: string; metric: string; value: string }> {
  if (!entry || typeof entry !== "object") return [];
  const item = entry as UnknownRecord;
  if (!item.metrics || typeof item.metrics !== "object") return [];
  return Object.entries(item.metrics as UnknownRecord).map(([metric, value]) => ({ key: `${String(item.result_id)}:${metric}`, result_id: String(item.result_id), metric, value: metricValue(value) }));
}

function incompatibleText(value: unknown): string {
  if (!Array.isArray(value)) return "无";
  return value.map((item) => item && typeof item === "object" ? `${String((item as UnknownRecord).result_id)}: ${JSON.stringify((item as UnknownRecord).reasons ?? [])}` : String(item)).join("; ");
}

function metricValue(value: unknown): string {
  if (value === undefined || value === null) return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function toPoints(value: unknown): PricePoint[] {
  if (Array.isArray(value)) return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const point = item as UnknownRecord;
    return typeof point.time === "string" && typeof point.value === "number" ? [{ time: point.time, value: point.value }] : [];
  });
  if (!value || typeof value !== "object") return [];
  return Object.entries(value as UnknownRecord).flatMap(([time, point]) => typeof point === "number" && Number.isFinite(point) ? [{ time, value: point }] : []);
}

function toAnalysisPoints(value: unknown): { label: string; value: number }[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const point = item as UnknownRecord;
    return typeof point.label === "string" && typeof point.value === "number" ? [{ label: point.label, value: point.value }] : [];
  });
}
