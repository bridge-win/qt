import { useMemo, useState, type ReactElement } from "react";
import { Alert, Card, Col, Descriptions, Drawer, Empty, Input, Row, Space, Spin, Table, Tabs } from "antd";
import type { CatalogSignal, Capability } from "../types";
import { PageTitle, type CatalogResponse, type UnknownRecord, useApi } from "../workbench";
import { QueryState } from "../components";

type LearningDocumentationResponse = {
  items: UnknownRecord[];
  next_cursor: string | null;
};

export function LearnPage(): ReactElement {
  const catalog = useApi<CatalogResponse>("catalog", "/api/v3/catalog");
  const indicators = useApi<{ items: Capability[] }>("indicators", "/api/v3/indicators");
  const learningDocs = useApi<LearningDocumentationResponse>("learning-docs", "/api/v3/learning-docs?limit=200");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<CatalogSignal | null>(null);
  const signals = useMemo(
    () => (catalog.data?.signals ?? []).filter((signal) => JSON.stringify(signal).toLowerCase().includes(search.toLowerCase())),
    [catalog.data?.signals, search],
  );
  const documentation = useMemo(
    () => learningDocs.data?.items.find((item) => item.id === selected?.id),
    [learningDocs.data?.items, selected?.id],
  );

  return (
    <>
      <PageTitle eyebrow="学习路径 · 指标 · 信号" title="理解条件、数据口径与证据">
        学习顺序：买入持有 / DCA → SMA、RSI、Donchian → 信号交叉 → 组合 → 防未来数据 → 插针 → 前向模拟。
      </PageTitle>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={16}>
          <Card title="50 个版本化信号">
            <Input.Search
              aria-label="搜索信号"
              placeholder="搜索信号、指标或条件"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <QueryState loading={catalog.isLoading} error={catalog.error} empty={signals.length === 0}>
              <Table<CatalogSignal>
                rowKey="id"
                size="small"
                pagination={{ pageSize: 10 }}
                dataSource={signals}
                onRow={(record) => ({ onClick: () => setSelected(record) })}
                columns={[
                  { title: "信号", dataIndex: "id" },
                  { title: "类别", dataIndex: "category", render: (value) => String(value ?? "—") },
                  { title: "方向", dataIndex: "direction", render: (value) => String(value ?? "—") },
                  { title: "判定", dataIndex: "evaluator", render: (value) => String(value ?? "—") },
                  { title: "所需指标", render: (_, row) => listText(row.required_indicators) },
                  { title: "默认参数", render: (_, row) => parameterSummary(row.parameters) },
                ]}
              />
            </QueryState>
          </Card>
        </Col>
        <Col xs={24} xl={8}>
          <Card title="指标运行边界">
            <QueryState loading={indicators.isLoading} error={indicators.error} empty={(indicators.data?.items.length ?? 0) === 0}>
              <Table<Capability>
                rowKey="id"
                size="small"
                pagination={{ pageSize: 6 }}
                dataSource={indicators.data?.items}
                columns={[
                  { title: "指标族", dataIndex: "name", render: (value, row) => String(value ?? row.id ?? "—") },
                  { title: "计算", dataIndex: "execution_status", render: (value) => String(value ?? "—") },
                  { title: "数据条件", dataIndex: "data_status", render: (value) => String(value ?? "—") },
                ]}
              />
            </QueryState>
          </Card>
        </Col>
      </Row>
      <Drawer open={selected !== null} title={selected?.id} onClose={() => setSelected(null)} width={620}>
        <SignalDocumentation signal={selected} documentation={documentation} loading={learningDocs.isLoading} error={learningDocs.error} />
      </Drawer>
    </>
  );
}

function SignalDocumentation({
  signal,
  documentation,
  loading,
  error,
}: {
  signal: CatalogSignal | null;
  documentation: UnknownRecord | undefined;
  loading: boolean;
  error: unknown;
}): ReactElement {
  if (!signal) return <Empty description="选择一个信号" />;

  const parameters = arrayRecords(documentation?.parameters ?? signal.parameters);
  const formula = displayValue(documentation?.formula_zh ?? documentation?.formula_en ?? signal.formula);
  const condition = displayValue(documentation?.condition_zh ?? documentation?.condition_en ?? signal.description);

  return (
    <>
      {loading && <div className="query-state"><Space><Spin /><span>正在读取信号说明…</span></Space></div>}
      {error && <Alert className="research-notice" type="warning" showIcon message="学习文档暂不可用" description="目录中的名称和默认参数仍可阅读；服务端文档恢复后可重新打开此信号。" />}
      {documentation && (
        <>
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="判定方式">{displayValue(signal.evaluator)}</Descriptions.Item>
            <Descriptions.Item label="类别 / 方向">{`${displayValue(signal.category)} / ${displayValue(signal.direction)}`}</Descriptions.Item>
            <Descriptions.Item label="公式">{formula}</Descriptions.Item>
            <Descriptions.Item label="条件">{condition}</Descriptions.Item>
            <Descriptions.Item label="所需指标">{listText(documentation.required_indicators ?? signal.required_indicators)}</Descriptions.Item>
            <Descriptions.Item label="可用时间">{teachingValue("available_at", documentation.available_at)}</Descriptions.Item>
            <Descriptions.Item label="预热">{teachingValue("warmup", documentation.warmup)}</Descriptions.Item>
            <Descriptions.Item label="交叉语义">{teachingValue("cross_semantics", documentation.cross_semantics)}</Descriptions.Item>
          </Descriptions>
          <Card size="small" className="top-gap" title="参数与阈值">
            <Table<UnknownRecord>
              size="small"
              rowKey="name"
              pagination={false}
              dataSource={parameters}
              columns={[
                { title: "参数", dataIndex: "name" },
                { title: "默认值", dataIndex: "default", render: (value) => displayValue(value) },
                { title: "范围", render: (_, row) => `${displayValue(row.minimum)} ～ ${displayValue(row.maximum)}` },
                { title: "类型", dataIndex: "type", render: (value) => displayValue(value, "数值") },
              ]}
            />
          </Card>
          <Tabs
            className="top-gap"
            items={[
              {
                key: "semantics",
                label: "有效性与限制",
                children: <Descriptions bordered size="small" column={1}>
                  <Descriptions.Item label="生效参数">{listText(documentation.effective_parameters)}</Descriptions.Item>
                  <Descriptions.Item label="忽略的旧参数">{listText(documentation.ignored_legacy_parameters)}</Descriptions.Item>
                  <Descriptions.Item label="缺失值行为">{teachingValue("nan_behavior", documentation.nan_behavior)}</Descriptions.Item>
                  <Descriptions.Item label="需要供应商可用时间">{displayValue(documentation.requires_provider_available_at)}</Descriptions.Item>
                  <Descriptions.Item label="可能修订">{displayValue(documentation.may_be_revised)}</Descriptions.Item>
                  <Descriptions.Item label="注意事项">{listText(documentation.caveats)}</Descriptions.Item>
                </Descriptions>,
              },
              {
                key: "raw",
                label: "原始文档（高级）",
                children: <pre className="response-preview">{JSON.stringify(documentation, null, 2)}</pre>,
              },
            ]}
          />
        </>
      )}
    </>
  );
}

function parameterSummary(value: unknown): string {
  return arrayRecords(value)
    .map((parameter) => `${String(parameter.name)}=${displayValue(parameter.default)}`)
    .join(" · ") || "—";
}

function arrayRecords(value: unknown): UnknownRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is UnknownRecord => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
}

function listText(value: unknown): string {
  return Array.isArray(value) && value.length > 0 ? value.map(String).join(" / ") : "—";
}

function displayValue(value: unknown, fallback = "—"): string {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

type TeachingField = "available_at" | "warmup" | "cross_semantics" | "nan_behavior";

const teachingTranslations: Record<TeachingField, Record<string, string>> = {
  available_at: {
    "derived after the completed market bar": "在该根市场 K 线收盘后计算得出；不会在未收盘的 K 线上提前可用。",
    "provider-recorded available_at required; may be revised": "必须使用数据供应商记录的可用时间；供应商后续可能修订该数据。",
  },
  warmup: {
    "Indicator-specific; inspect the concrete documentation.": "预热要求取决于具体指标；请查看该指标的具体说明。",
    "Rolling/EWM inputs use min_periods=1; early values are not a full-window confirmation.": "滚动窗口与 EWM 从最少 1 个输入开始计算；早期数值不代表已经完成完整窗口确认。",
  },
  cross_semantics: {
    "cross-down means current left < right and previous left >= previous right": "向下穿越：当前左侧 < 右侧，且上一根左侧 ≥ 右侧。",
    "cross-up means current left > right and previous left <= previous right": "向上穿越：当前左侧 > 右侧，且上一根左侧 ≤ 右侧。",
  },
  nan_behavior: {
    "Any NaN reaching the boolean evaluator becomes False.": "任何传入布尔判定器的 NaN 都会按 False（不触发信号）处理。",
    "Indicator-specific; missing input must not be interpreted as a true signal.": "取决于具体指标；缺失输入绝不能被解释为 True（触发信号）。",
    "Zero standard deviation is converted to z-score 0; final boolean NaN is False.": "标准差为零时 z-score 记为 0；最终布尔判定中的 NaN 按 False（不触发信号）处理。",
  },
};

function teachingValue(field: TeachingField, value: unknown): string {
  const source = displayValue(value);
  if (source === "—") return source;
  const translated = teachingTranslations[field][source];
  return translated ? `${translated}\n原文：${source}` : source;
}
