import { lazy, Suspense, useMemo, useState, type ReactElement, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Badge, Button, Card, Col, ConfigProvider, Descriptions, Empty, Form, Input, InputNumber, Layout, Menu, Modal, Progress, Row, Segmented, Select, Space, Statistic, Table, Tabs, Typography, type MenuProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ApiError, apiErrorText, post, request } from "./api";
import { ApiAction, QueryState, ReadOnlyNotice, StatusTag } from "./components";
import { PriceChart, type PricePoint } from "./charts/PriceChart";
import type { Dataset, Job, Runtime, Source } from "./types";
import { CapabilityResponse, CatalogResponse, PageTitle, type UnknownRecord, useApi } from "./workbench";
import { StrategiesPage } from "./pages/StrategiesPage";
import { LearnPage } from "./pages/LearnPage";
import { DataPage } from "./pages/DataPage";
import { ExperimentsPage } from "./pages/ExperimentsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { ComparePage } from "./pages/ComparePage";

const { Content, Sider } = Layout;
const { Paragraph, Text } = Typography;
const AnalysisChart = lazy(() => import("./charts/AnalysisChart"));

type ExperimentFormValues = {
  strategyVersionId: string;
  datasetId: string;
  precision: "bar" | "trade" | "order_book";
  market: "spot" | "perpetual";
  validation: "quick" | "standard";
  feeBps: number;
  spreadBps: number;
  slippageBps: number;
  fundingBps: number;
  leverage: number;
  initialCash: number;
  seed: number;
  from?: string;
  to?: string;
};

const navItems: { key: string; label: string }[] = [
  { key: "/", label: "研究总览" }, { key: "/learn", label: "学习与指标" }, { key: "/data", label: "数据中心" },
  { key: "/strategies", label: "策略与规则" }, { key: "/experiments", label: "实验任务" }, { key: "/compare", label: "比较中心" },
  { key: "/traces", label: "历史决策" }, { key: "/wicks", label: "插针事件" }, { key: "/portfolio", label: "机会与组合" },
  { key: "/validation", label: "搜索与验证" }, { key: "/runtime", label: "模拟与运行" }, { key: "/archive", label: "研究档案" },
];


function endpointError(path: string, purpose: string): ReactElement {
  return <Alert type="warning" showIcon message="API 能力尚未连接" description={<><code>{path}</code> 是 {purpose} 的约定入口，但当前服务未提供可用响应。界面不会用本地估算或示例记录替代它。</>} />;
}

function AppShell(): ReactElement {
  const location = useLocation();
  const navigate = useNavigate();
  const runtime = useApi<Runtime>("runtime", "/api/v3/runtime", { refetchInterval: 30_000 });
  const menuItems: MenuProps["items"] = navItems.map((item) => ({ key: item.key, label: item.label }));
  return <Layout className="workbench-shell"><Sider breakpoint="lg" collapsedWidth="0" theme="dark" className="sidebar">
    <Link to="/" className="brand"><span>QT</span><div>研究工作台<small>evidence first</small></div></Link>
    <Menu theme="dark" mode="inline" selectedKeys={[location.pathname]} items={menuItems} onClick={({ key }) => navigate(key)} />
    <div className="sidebar-status"><Badge status={runtime.data?.worker.online ? "success" : "warning"} text={runtime.data?.worker.online ? "研究 worker 在线" : "worker 状态未知"} /><br /><Text type="secondary">{runtime.data?.mode ?? "正在确认模式"} · live {String(runtime.data?.live_enabled ?? false)}</Text></div>
  </Sider><Layout><Content className="content">{runtime.error && <ServiceOfflineBanner />}<Routes>
    <Route path="/" element={<OverviewPage />} /><Route path="/learn" element={<LearnPage />} /><Route path="/data" element={<DataPage />} />
    <Route path="/strategies" element={<StrategiesPage />} /><Route path="/experiments" element={<ExperimentsPage />} /><Route path="/compare" element={<ComparePage />} />
    <Route path="/traces" element={<TracePage />} /><Route path="/wicks" element={<WicksPage />} />
    <Route path="/portfolio" element={<PortfolioPage />} /><Route path="/validation" element={<ValidationPage />} />
    <Route path="/runtime" element={<RuntimePage />} /><Route path="/archive" element={<ArchivePage />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></Content></Layout></Layout>;
}

function ServiceOfflineBanner(): ReactElement {
  return <Alert className="service-offline-banner" type="error" showIcon message="研究服务暂时不可用" description={<div>页面导航和说明仍可使用，但实时数据、策略保存与实验提交会在服务恢复前暂停。<br />本机开发可在项目根目录启动研究服务，并访问 <code>http://127.0.0.1:8877/api/v3/runtime</code> 确认状态；恢复后点击“重新读取”。</div>} action={<Button size="small" danger onClick={() => window.location.reload()}>重新读取</Button>} />;
}

function TracePage(): ReactElement {
  const results = useApi<{ items: UnknownRecord[] }>("results", "/api/v3/results?limit=100");
  const [resultId, setResultId] = useState(""); const [selected, setSelected] = useState<UnknownRecord | null>(null); const [cursor, setCursor] = useState<number | null>(null);
  const cursorQuery = cursor === null ? "" : `&cursor=${cursor}`;
  const query = useApi<{ items: UnknownRecord[]; next_cursor?: number | null }>(`traces:${resultId}:${cursor ?? "start"}`, `/api/v3/results/${encodeURIComponent(resultId)}/traces?window=100${cursorQuery}`, { enabled: Boolean(resultId) });
  return <><PageTitle eyebrow="只读当时可见数据" title="历史决策回放"><Paragraph>选择已发布结果后，按原始时间戳读取 native DecisionTrace：原因、未交易原因、当时观察值及 intent 数量均由 worker 记录。回放不补造事后条件或订单。</Paragraph></PageTitle><Card><Select className="full-width" aria-label="选择回放结果" value={resultId || undefined} onChange={(value) => { setResultId(value); setCursor(null); setSelected(null); }} options={(results.data?.items ?? []).map((item) => ({ value: String(item.result_id ?? item.run_id ?? item.id), label: `${String(item.result_id ?? item.run_id ?? item.id)} · ${String(item.status ?? "已发布")}` }))} placeholder="选择已发布结果" />{!resultId ? <Empty description="选择结果后读取完整分页 DecisionTrace" /> : <QueryState loading={query.isLoading} error={query.error} empty={(query.data?.items.length ?? 0) === 0}><Table rowKey={(row, index) => String(row.sequence ?? index)} onRow={(record) => ({ onClick: () => setSelected(record.trace && typeof record.trace === "object" ? record.trace as UnknownRecord : record) })} dataSource={query.data?.items} columns={[{ title: "序号", dataIndex: "sequence" }, { title: "timestamp", render: (_, row) => String(recordValue(row, "trace", "timestamp") ?? recordValue(row, "trace", "known_at") ?? "—") }, { title: "原因", render: (_, row) => countValue(recordValue(row, "trace", "reasons")) }, { title: "未交易原因", render: (_, row) => String(recordValue(row, "trace", "no_trade_cause") ?? "—") }, { title: "意图", render: (_, row) => String(recordValue(row, "trace", "intent_count") ?? "0") }]} pagination={false} /></QueryState>}<Space className="page-actions"><Button disabled={cursor === null || query.isLoading} onClick={() => setCursor(null)}>首页</Button><Button type="primary" disabled={query.data?.next_cursor === null || query.data?.next_cursor === undefined || query.isLoading} onClick={() => setCursor(query.data?.next_cursor ?? null)}>下一页</Button></Space></Card><TraceDetail trace={selected} /></>;
}

function ValidationPage(): ReactElement {
  const [mode, setMode] = useState<"optimization" | "validation">("optimization");
  const [strategyId, setStrategyId] = useState("");
  const strategies = useApi<{ items: UnknownRecord[] }>("strategies", "/api/v3/strategies?limit=100");
  const versions = useApi<{ items: UnknownRecord[] }>(`versions:${strategyId}`, `/api/v3/strategies/${encodeURIComponent(strategyId)}/versions`, { enabled: Boolean(strategyId) });
  const experiments = useApi<{ items: UnknownRecord[] }>("experiments", "/api/v3/experiments?limit=100");
  const [form] = Form.useForm<{ strategyVersionId: string; experiment: string; searchSpace: string; sampler: "grid" | "random" | "tpe"; objective: "sharpe" | "calmar" | "net_return"; budget: number; seed: number; profile: "quick" | "standard" }>();
  const start = useMutation({ mutationFn: async (values: { strategyVersionId: string; experiment: string; searchSpace: string; sampler: "grid" | "random" | "tpe"; objective: "sharpe" | "calmar" | "net_return"; budget: number; seed: number; profile: "quick" | "standard" }) => {
    const experiment = parseObject(values.experiment, "实验规格");
    if (mode === "validation") return post<{ job: Job }>("/api/v3/validation", { strategy_version_id: values.strategyVersionId, experiment, profile: values.profile, seed: values.seed }, { "Idempotency-Key": crypto.randomUUID() });
    const searchSpace = parseSearchSpace(values.searchSpace);
    return post<{ job: Job }>("/api/v3/optimizations", { strategy_version_id: values.strategyVersionId, experiment, search_space: searchSpace, sampler: values.sampler, objective: values.objective, budget: values.budget, seed: values.seed }, { "Idempotency-Key": crypto.randomUUID() });
  } });
  const selectExperiment = (value: string): void => { const found = experiments.data?.items.find((item) => String(item.job_id ?? item.experiment_id) === value); const payload = found?.payload; if (payload && typeof payload === "object") form.setFieldValue("experiment", JSON.stringify(payload, null, 2)); };
  return <><PageTitle eyebrow="候选、稳健性与样本外证据" title="搜索与验证"><Paragraph>选择服务端保存的策略版本和实验规格；搜索空间、采样器、目标、预算、种子与验证档案均会固定在后台任务中。</Paragraph></PageTitle><Card><Segmented value={mode} onChange={(value) => setMode(value as "optimization" | "validation")} options={[{ value: "optimization", label: "参数搜索" }, { value: "validation", label: "严格验证" }]} /><Form form={form} layout="vertical" className="top-gap" initialValues={{ sampler: "tpe", objective: "sharpe", budget: 50, seed: 7, profile: "standard", searchSpace: '{\n  "fast": [10, 20, 30],\n  "slow": [50, 80, 120]\n}' }} onFinish={(values) => start.mutate(values)}><Row gutter={16}><Col xs={24} md={12}><Form.Item label="策略" required><Select value={strategyId || undefined} onChange={(value) => { setStrategyId(value); form.setFieldValue("strategyVersionId", undefined); }} options={(strategies.data?.items ?? []).filter((item) => item.read_only !== true).map((item) => ({ value: String(item.strategy_id), label: String(item.name ?? item.strategy_id) }))} placeholder="选择已创建的策略" /></Form.Item></Col><Col xs={24} md={12}><Form.Item name="strategyVersionId" label="不可变版本" rules={[{ required: true, message: "选择一个已保存的版本" }]}><Select loading={versions.isLoading} options={(versions.data?.items ?? []).map((item) => ({ value: String(item.version_id), label: `v${String(item.revision)} · ${String(item.message ?? "")}` }))} placeholder="先选择策略" /></Form.Item></Col></Row><Form.Item label="从已有实验载入规格"><Select allowClear onChange={selectExperiment} options={(experiments.data?.items ?? []).map((item) => ({ value: String(item.job_id ?? item.experiment_id), label: `${String(item.job_id ?? item.experiment_id)} · ${String(item.status ?? "")}` }))} placeholder="选择后填入下方实验 JSON" /></Form.Item><Form.Item name="experiment" label="实验规格" rules={[{ required: true, message: "从已保存实验载入，或填写完整实验规格" }]}><Input.TextArea aria-label="实验规格" autoSize={{ minRows: 6 }} placeholder='{ "dataset_id": "…", "from": "…", "to": "…", "costs": { … } }' /></Form.Item>{mode === "optimization" && <><Row gutter={16}><Col xs={24} md={12}><Form.Item name="searchSpace" label="有界搜索空间" rules={[{ required: true }]}><Input.TextArea aria-label="搜索空间" autoSize={{ minRows: 6 }} /></Form.Item></Col><Col xs={24} md={12}><Form.Item name="sampler" label="采样器"><Select options={[{ value: "grid", label: "网格" }, { value: "random", label: "随机" }, { value: "tpe", label: "TPE" }]} /></Form.Item><Form.Item name="objective" label="目标"><Select options={[{ value: "sharpe", label: "Sharpe" }, { value: "calmar", label: "Calmar" }, { value: "net_return", label: "净收益" }]} /></Form.Item><Form.Item name="budget" label="试验预算" rules={[{ required: true }]}><InputNumber min={1} max={500} className="full-width" /></Form.Item></Col></Row></>}{mode === "validation" && <Form.Item name="profile" label="验证档案"><Select options={[{ value: "quick", label: "快速" }, { value: "standard", label: "标准" }]} /></Form.Item>}<Form.Item name="seed" label="随机种子" rules={[{ required: true }]}><InputNumber min={0} className="full-width" /></Form.Item><ApiAction label={mode === "optimization" ? "创建参数搜索" : "创建验证任务"} loading={start.isPending} onClick={() => form.submit()} /></Form>{start.isError && <Alert type="error" showIcon className="research-notice" message="任务未创建" description={apiErrorText(start.error)} />}{start.data && <Alert type="success" showIcon className="research-notice" message={`后台任务已创建：${start.data.job.job_id}`} description="服务端将保存每个试验、候选与验证证据。" />}</Card></>;
}

function ArchivePage(): ReactElement {
  const artifacts = useApi<{ items: UnknownRecord[] }>("artifacts", "/api/v3/artifacts?limit=100");
  const strategies = useApi<{ items: UnknownRecord[] }>("strategies", "/api/v3/strategies?limit=100");
  const [strategyId, setStrategyId] = useState(""); const [versionId, setVersionId] = useState(""); const [body, setBody] = useState("");
  const versions = useApi<{ items: UnknownRecord[] }>(`versions:${strategyId}`, `/api/v3/strategies/${encodeURIComponent(strategyId)}/versions`, { enabled: Boolean(strategyId) });
  const note = useMutation({ mutationFn: () => post<UnknownRecord>("/api/v3/notes", { entity_type: "strategy_version", entity_id: versionId, strategy_version_id: versionId, body }, { "Idempotency-Key": crypto.randomUUID() }) });
  return <><PageTitle eyebrow="报告、数据指纹、失败案例" title="研究档案"><Paragraph>工件由鉴权 Worker 从私有 R2 提供。下载链接/内容必须由后端索引确认，档案页面不会猜测对象键或绕过授权。</Paragraph></PageTitle><Row gutter={[16, 16]}><Col xs={24} xl={15}><Card title="工件索引"><QueryState loading={artifacts.isLoading} error={artifacts.error} empty={(artifacts.data?.items.length ?? 0) === 0}><Table rowKey={(row, index) => String(row.artifact_id ?? row.id ?? index)} dataSource={artifacts.data?.items} columns={genericColumns([])} /></QueryState></Card></Col><Col xs={24} xl={9}><Card title="研究笔记"><Select className="full-width" value={strategyId || undefined} onChange={(value) => { setStrategyId(value); setVersionId(""); }} options={(strategies.data?.items ?? []).filter((item) => item.read_only !== true).map((item) => ({ value: String(item.strategy_id), label: String(item.name ?? item.strategy_id) }))} placeholder="选择策略" /><Select className="full-width top-gap" value={versionId || undefined} onChange={setVersionId} options={(versions.data?.items ?? []).map((item) => ({ value: String(item.version_id), label: `v${String(item.revision)} · ${String(item.message ?? "")}` }))} placeholder="选择不可变版本" /><Input.TextArea className="top-gap" value={body} onChange={(event) => setBody(event.target.value)} placeholder="记录假设、结论或失败原因" autoSize={{ minRows: 6 }} aria-label="研究笔记" /><Space className="page-actions"><ApiAction label="保存版本化笔记" loading={note.isPending} disabled={!versionId || !body} onClick={() => note.mutate()} /></Space>{note.isError && <Alert type="error" showIcon message="笔记未保存" description={apiErrorText(note.error)} />}{note.data && <Alert type="success" showIcon message="笔记已由服务端保存" />}</Card></Col></Row></>;
}

function WindowedEndpointPage({ title, endpoint, purpose }: { title: string; endpoint: string; purpose: string }): ReactElement {
  const [from, setFrom] = useState(""); const [to, setTo] = useState("");
  const query = useApi<{ items: UnknownRecord[] }>(`window:${endpoint}:${from}:${to}`, `${endpoint}?limit=100&max_points=500${from ? `&from=${encodeURIComponent(from)}` : ""}${to ? `&to=${encodeURIComponent(to)}` : ""}`);
  return <><PageTitle eyebrow="时间窗读取" title={title}><Paragraph>{purpose}。请求限定为最多 100 条记录或 500 个绘图点；更大范围必须由服务端汇总或导出。</Paragraph></PageTitle><Card><Space wrap><Input aria-label="开始时间" value={from} onChange={(event) => setFrom(event.target.value)} placeholder="from: ISO 8601" /><Input aria-label="结束时间" value={to} onChange={(event) => setTo(event.target.value)} placeholder="to: ISO 8601" /></Space><QueryState loading={query.isLoading} error={query.error} empty={(query.data?.items.length ?? 0) === 0}><Table rowKey={(row, index) => String(row.id ?? row.trace_id ?? index)} scroll={{ x: 900 }} dataSource={query.data?.items} columns={genericColumns([])} /></QueryState></Card></>;
}
function WicksPage(): ReactElement {
  const datasets = useApi<{ items: Dataset[] }>("datasets", "/api/v3/datasets");
  const [datasetId, setDatasetId] = useState(""); const [from, setFrom] = useState(""); const [to, setTo] = useState("");
  const enabled = Boolean(datasetId && from && to);
  const events = useApi<{ items: UnknownRecord[] }>(`wicks:${datasetId}:${from}:${to}`, `/api/v3/events/wicks?dataset_id=${encodeURIComponent(datasetId)}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&limit=100`, { enabled });
  return <><PageTitle eyebrow="持久化 OHLCV 证据" title="插针事件"><Paragraph>选择数据集及带时区的开始/结束时间。服务端最多接受 366 天窗口，并返回挂单、确认与恢复的实际证据；无数据不生成示例事件。</Paragraph></PageTitle><Card><Row gutter={16}><Col xs={24} md={8}><Select className="full-width" value={datasetId || undefined} onChange={setDatasetId} options={(datasets.data?.items ?? []).map((item) => ({ value: item.dataset_id, label: item.dataset_id }))} placeholder="选择数据集" /></Col><Col xs={24} md={8}><Input value={from} onChange={(event) => setFrom(event.target.value)} placeholder="2026-01-01T00:00:00+08:00" /></Col><Col xs={24} md={8}><Input value={to} onChange={(event) => setTo(event.target.value)} placeholder="2026-02-01T00:00:00+08:00" /></Col></Row>{!enabled ? <Empty className="top-gap" description="选择数据集与完整时区时间窗以读取插针证据" /> : <QueryState loading={events.isLoading} error={events.error} empty={(events.data?.items.length ?? 0) === 0}><Table rowKey={(row, index) => String(row.event_id ?? index)} dataSource={events.data?.items} columns={[{ title: "事件", dataIndex: "event_id" }, { title: "时间", dataIndex: "timestamp" }, { title: "恢复", dataIndex: "recovery_time" }, { title: "成交证据", dataIndex: "fill_status" }]} /></QueryState>}</Card></>;
}

function PortfolioPage(): ReactElement {
  const opportunities = useApi<{ items: UnknownRecord[] }>("opportunities", "/api/v3/opportunities?limit=100");
  const portfolio = useApi<UnknownRecord>("portfolio", "/api/v3/portfolio");
  const [selected, setSelected] = useState<UnknownRecord | null>(null);
  return <><PageTitle eyebrow="扫描 · 账本 · 风险敞口" title="机会与组合"><Paragraph>机会评分原因、仓位、PnL、资金使用和贡献都来自后端账本。没有 API 结果时，不显示预测或模拟金额。</Paragraph></PageTitle><Tabs items={[{ key: "opportunities", label: "机会扫描", children: <><QueryState loading={opportunities.isLoading} error={opportunities.error} empty={(opportunities.data?.items.length ?? 0) === 0}><Table rowKey={(row, index) => String(row.id ?? index)} onRow={(record) => ({ onClick: () => setSelected(record) })} dataSource={opportunities.data?.items} columns={[{ title: "机会", dataIndex: "id", render: (value, row) => String(value ?? row.symbol ?? row.name ?? "—") }, { title: "状态", dataIndex: "status", render: (value) => <StatusTag value={value as string} /> }, { title: "评分", dataIndex: "score", render: (value) => value ?? "服务未提供" }, { title: "原因", dataIndex: "reason", render: (value) => value ?? "服务未提供" }]} /></QueryState><OpportunityDetail item={selected} /></> }, { key: "portfolio", label: "组合与风险", children: <QueryState loading={portfolio.isLoading} error={portfolio.error}><PortfolioDetail portfolio={portfolio.data ?? {}} /></QueryState> }]} /></>;
}

function RuntimePage(): ReactElement {
  const runtime = useApi<Runtime>("runtime", "/api/v3/runtime", { refetchInterval: 15_000 });
  const [commandOpen, setCommandOpen] = useState(false); const [command, setCommand] = useState("paper.pause");
  const mutate = useMutation({ mutationFn: () => post<UnknownRecord>("/api/v3/runtime/commands", { command }, { "Idempotency-Key": crypto.randomUUID() }) });
  return <><PageTitle eyebrow="研究 / paper / live 隔离" title="模拟与运行"><Paragraph>实时命令必须在后端白名单、审计和模式策略下执行。此 UI 不提供启用 live 的本地开关。</Paragraph><Button onClick={() => setCommandOpen(true)}>提交受约束 paper 命令</Button></PageTitle><QueryState loading={runtime.isLoading} error={runtime.error}><Descriptions bordered column={1}><Descriptions.Item label="模式"><StatusTag value={runtime.data?.mode} /></Descriptions.Item><Descriptions.Item label="Live 可用"><StatusTag value={runtime.data?.live_enabled} /></Descriptions.Item><Descriptions.Item label="Worker"><StatusTag value={runtime.data?.worker.online} /></Descriptions.Item><Descriptions.Item label="可用内存">{runtime.data?.available_memory_mib ?? "服务未提供"} MiB</Descriptions.Item></Descriptions></QueryState><Modal open={commandOpen} title="paper 运行命令" onCancel={() => setCommandOpen(false)} onOk={() => mutate.mutate()} okText="提交并记录审计"><Select className="full-width" value={command} onChange={setCommand} options={[{ value: "paper.pause", label: "暂停 paper 新入场" }, { value: "paper.resume", label: "恢复 paper 新入场" }]} />{mutate.isError && <Alert type="error" className="research-notice" message="命令未执行" description={apiErrorText(mutate.error)} />}{mutate.data && <pre className="response-preview">{JSON.stringify(mutate.data, null, 2)}</pre>}</Modal></>;
}

function Metric({ title, value, suffix }: { title: string; value: string | number; suffix?: string }): ReactElement { return <Col xs={24} sm={8}><Card size="small"><Statistic title={title} value={value} suffix={suffix} /></Card></Col>; }

function genericColumns(preferred: string[]): ColumnsType<UnknownRecord> {
  const keys = preferred.length > 0 ? preferred : ["id", "name", "status", "created_at", "updated_at"];
  return keys.map((key) => ({ title: key, dataIndex: key, render: (value: unknown) => typeof value === "object" && value !== null ? <Text ellipsis={{ tooltip: JSON.stringify(value) }}>{JSON.stringify(value)}</Text> : String(value ?? "—") }));
}

function buildStrategyContent(kind: "rules" | "plugin", rules: string, code: string): UnknownRecord {
  if (kind === "plugin") return { kind: "plugin", code, plugin_api_version: "v1" };
  const parsed = parseObject(rules, "规则组合");
  if (!("entry" in parsed) || !("exit" in parsed)) throw new Error("规则组合必须同时包含 entry 和 exit。" );
  return { kind: "rules", rules: parsed };
}
function parseObject(value: string, label: string): UnknownRecord {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label}必须是非空 JSON 对象。`);
  if (Object.keys(parsed).length === 0) throw new Error(`${label}不能为空。`);
  return parsed as UnknownRecord;
}
function parseSearchSpace(value: string): Record<string, Array<string | number | boolean>> {
  const parsed = parseObject(value, "搜索空间");
  const entries = Object.entries(parsed);
  if (entries.length === 0 || entries.length > 30) throw new Error("搜索空间需要 1 至 30 个参数。");
  const normalized: Record<string, Array<string | number | boolean>> = {};
  for (const [name, choices] of entries) {
    if (!Array.isArray(choices) || choices.length === 0 || !choices.every((choice) => typeof choice === "string" || typeof choice === "number" || typeof choice === "boolean")) throw new Error(`搜索空间 ${name} 必须是非空的字符串、数字或布尔值数组。`);
    normalized[name] = choices as Array<string | number | boolean>;
  }
  return normalized;
}
function TraceDetail({ trace }: { trace: UnknownRecord | null }): ReactElement {
  if (!trace) return <Card className="top-gap" title="决策细节"><Empty description="选择一条决策记录以查看当时已知数据和订单。" /></Card>;
  const observed = trace.observed_values && typeof trace.observed_values === "object" ? trace.observed_values as UnknownRecord : null;
  const intentCount = typeof trace.intent_count === "number" ? trace.intent_count : 0;
  return <Card className="top-gap" title="因果证据"><div className="causal-strip" aria-label="数据可用到结果的因果证据链"><EvidenceStep label="数据可用" value={String(trace.timestamp ?? trace.known_at ?? "时间未记录")} detail={observed ? "已记录当时观察值" : "观察值未记录"} /><EvidenceStep label="条件与原因" value={compactEvidence(trace.reasons)} detail={observed ? observationSummary(observed) : "没有观察值"} /><EvidenceStep label="委托 / 成交意图" value={intentCount > 0 ? `${intentCount} 个意图` : "未产生意图"} detail={String(trace.no_trade_cause ?? (intentCount > 0 ? "原因见下方" : "没有记录原因"))} /><EvidenceStep label="结果" value={intentCount > 0 ? "由任务结果汇总" : "本事件未交易"} detail="收益、费用和回撤只在结果记录中计算" /></div><Tabs className="top-gap" items={[{ key: "evidence", label: "证据字段", children: <Descriptions bordered size="small" column={1}><Descriptions.Item label="时间">{String(trace.timestamp ?? trace.known_at ?? "—")}</Descriptions.Item><Descriptions.Item label="原因">{renderValue(trace.reasons ?? "服务端未提供")}</Descriptions.Item><Descriptions.Item label="未交易原因">{renderValue(trace.no_trade_cause ?? "—")}</Descriptions.Item><Descriptions.Item label="意图数量">{String(intentCount)}</Descriptions.Item><Descriptions.Item label="当时观察值">{renderValue(observed ?? "服务端未提供")}</Descriptions.Item></Descriptions> }, { key: "raw", label: "原始记录（高级）", children: <pre className="response-preview">{JSON.stringify(trace, null, 2)}</pre> }]} /></Card>;
}
function EvidenceStep({ label, value, detail }: { label: string; value: string; detail: string }): ReactElement { return <section className="causal-step"><Text className="causal-label">{label}</Text><strong>{value}</strong><Text type="secondary">{detail}</Text></section>; }
function compactEvidence(value: unknown): string { if (typeof value === "string" && value) return value; if (Array.isArray(value)) return value.length > 0 ? `${value.length} 条原因` : "没有原因"; return value && typeof value === "object" ? "已记录条件" : "条件未记录"; }
function observationSummary(value: UnknownRecord): string { const entries = Object.entries(value).slice(0, 3); return entries.length > 0 ? entries.map(([name, observed]) => `${name}=${String(observed)}`).join(" · ") : "没有观察值"; }
function OpportunityDetail({ item }: { item: UnknownRecord | null }): ReactElement {
  if (!item) return <Card size="small" className="top-gap"><Empty description="选择一项机会以查看评分依据和数据质量。" /></Card>;
  return <Card size="small" className="top-gap" title="机会证据"><Descriptions bordered size="small" column={1}><Descriptions.Item label="评分">{String(item.score ?? "服务未提供")}</Descriptions.Item><Descriptions.Item label="评分原因">{renderValue(item.reasons ?? item.reason ?? "服务未提供")}</Descriptions.Item><Descriptions.Item label="数据质量">{renderValue(item.data_quality ?? item.quality ?? "服务未提供")}</Descriptions.Item><Descriptions.Item label="有效时间">{String(item.available_at ?? item.expires_at ?? "服务未提供")}</Descriptions.Item></Descriptions></Card>;
}
function PortfolioDetail({ portfolio }: { portfolio: UnknownRecord }): ReactElement {
  const positions = Array.isArray(portfolio.positions) ? portfolio.positions as UnknownRecord[] : [];
  const contributions = Array.isArray(portfolio.contributions) ? portfolio.contributions as UnknownRecord[] : [];
  return <><Descriptions bordered size="small" column={1}><Descriptions.Item label="账本状态">{String(portfolio.status ?? portfolio.mode ?? "服务未提供")}</Descriptions.Item><Descriptions.Item label="风险敞口">{renderValue(portfolio.risk_exposure ?? portfolio.risk ?? "服务未提供")}</Descriptions.Item><Descriptions.Item label="资金使用">{renderValue(portfolio.capital_usage ?? "服务未提供")}</Descriptions.Item></Descriptions><Row gutter={[16, 16]} className="top-gap"><Col xs={24} xl={12}><Card size="small" title="持仓"><Table rowKey={(_, index) => String(index)} pagination={false} dataSource={positions} columns={genericColumns([])} /></Card></Col><Col xs={24} xl={12}><Card size="small" title="策略贡献"><Table rowKey={(_, index) => String(index)} pagination={false} dataSource={contributions} columns={genericColumns([])} /></Card></Col></Row></>;
}
function recordValue(row: UnknownRecord, nested: string, key: string): unknown { const container = row[nested]; return container && typeof container === "object" ? (container as UnknownRecord)[key] : row[key]; }
function countValue(value: unknown): string { return Array.isArray(value) ? String(value.length) : value === undefined || value === null ? "—" : "1"; }
function renderValue(value: unknown): ReactNode { return typeof value === "object" && value !== null ? <pre className="inline-json">{JSON.stringify(value, null, 2)}</pre> : String(value); }
function toAnalysisPoints(value: unknown): { label: string; value: number }[] { if (!Array.isArray(value)) return []; return value.flatMap((item) => { if (!item || typeof item !== "object") return []; const point = item as Record<string, unknown>; return typeof point.label === "string" && typeof point.value === "number" ? [{ label: point.label, value: point.value }] : []; }); }

export default function WorkbenchApp(): ReactElement {
  return <ConfigProvider theme={{ token: { colorPrimary: "#4f7cff", borderRadius: 6, fontFamily: "Inter, PingFang SC, -apple-system, BlinkMacSystemFont, sans-serif" } }}><AntApp><BrowserRouter><AppShell /></BrowserRouter></AntApp></ConfigProvider>;
}
