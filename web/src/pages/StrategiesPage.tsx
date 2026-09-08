import { lazy, Suspense, useEffect, useMemo, useState, type ReactElement } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Col, Drawer, Form, Input, InputNumber, Row, Segmented, Select, Space, Switch, Table, Typography } from "antd";
import { ApiError, apiErrorText, post } from "../api";
import { ApiAction, QueryState } from "../components";
import { PageTitle, parseObject, type UnknownRecord, useApi } from "../workbench";

const { Paragraph, Text } = Typography;
const CodeEditor = lazy(() => import("../editor/CodeEditor"));

type StrategyKind = "rules" | "plugin" | "ensemble" | "regime_switch";
type EnsembleDraft = { strategyVersionId: string; targetWeight: number };
type RegimeDraft = { name: string; strategyVersionId: string; when: string };

const defaultRules = `{
  "entry": {"kind": "comparison", "left": {"indicator": "rsi", "parameters": {"period": 14}}, "comparator": "<", "right": 30},
  "exit": {"kind": "comparison", "left": {"indicator": "rsi", "parameters": {"period": 14}}, "comparator": ">", "right": 55}
}`;

// This matches tests/test_plugin_runtime.py: allowed imports and a real
// btc_backtest TargetWeightStrategy returned by build_strategy(parameters).
const verifiedPlugin = `from decimal import Decimal
from btc_backtest.strategies.base import StrategyMetadata, StrategyContext
from btc_backtest.strategies.target_weight import TargetWeightStrategy

class ConfiguredLongOnly(TargetWeightStrategy):
    metadata = StrategyMetadata(
        id="plugin_configured_long_only",
        version="1",
        description="Bounded long-only example with an explicit allocation parameter.",
        warmup_bars=0,
        supported_timeframes=("1h",),
    )

    def target_weight(self, context: StrategyContext) -> Decimal:
        return Decimal(str(self.parameters.get("allocation", "1")))

def build_strategy(parameters):
    return ConfiguredLongOnly(parameters)
`;

function buildStrategyContent(kind: StrategyKind, rules: string, code: string, ensemble: EnsembleDraft[], regimes: RegimeDraft[]): UnknownRecord {
  if (kind === "plugin") return { kind: "plugin", code, plugin_api_version: "1", parameters: [{ name: "allocation", value: 1, minimum: 0, maximum: 1, step: 0.05, description: "Target fraction of research equity (0–1)." }] };
  if (kind === "ensemble") {
    if (ensemble.length < 2 || ensemble.length > 3) throw new Error("An ensemble needs two or three immutable versions.");
    if (ensemble.some((member) => !member.strategyVersionId || member.targetWeight <= 0 || member.targetWeight > 1)) throw new Error("Every ensemble member needs a version and a weight between 0 and 1.");
    if (Math.abs(ensemble.reduce((sum, member) => sum + member.targetWeight, 0) - 1) > 0.000001) throw new Error("Ensemble target weights must sum exactly to 1.");
    return { kind: "ensemble", ensemble: ensemble.map((member) => ({ strategy_version_id: member.strategyVersionId, target_weight: member.targetWeight })) };
  }
  if (kind === "regime_switch") {
    if (regimes.length < 1 || regimes.length > 3) throw new Error("Regime switching needs one to three branches.");
    return { kind: "regime_switch", regimes: regimes.map((branch) => {
      if (!branch.name || !branch.strategyVersionId) throw new Error("Every regime branch needs a name and immutable version.");
      return { name: branch.name, strategy_version_id: branch.strategyVersionId, when: parseObject(branch.when, `Regime ${branch.name}`) };
    }) };
  }
  const parsed = parseObject(rules, "规则组合");
  if (!("entry" in parsed) || !("exit" in parsed)) throw new Error("规则组合必须同时包含 entry 和 exit。");
  return { kind: "rules", rules: parsed };
}

function contentKind(content: unknown): StrategyKind | "builtin" | null {
  if (!content || typeof content !== "object") return null;
  const mode = (content as UnknownRecord).mode ?? (content as UnknownRecord).kind;
  return mode === "rules" || mode === "plugin" || mode === "ensemble" || mode === "regime_switch" || mode === "builtin" ? mode : null;
}

function buildBuiltinContent(content: UnknownRecord | undefined, parameters: UnknownRecord): UnknownRecord {
  if (!content || typeof content.builtin_identity !== "string" || typeof content.builtin_version !== "string") throw new Error("The builtin identity and version must be loaded before parameter editing.");
  return { kind: "builtin", builtin_identity: content.builtin_identity, builtin_version: content.builtin_version, builtin_parameters: parameters };
}

export function StrategiesPage(): ReactElement {
  const { message } = AntApp.useApp();
  const queryClient = useQueryClient();
  const strategies = useApi<{ items: UnknownRecord[] }>("strategies", "/api/v3/strategies?limit=100");
  const [createOpen, setCreateOpen] = useState(false);
  const [strategyId, setStrategyId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [kind, setKind] = useState<StrategyKind>("rules");
  const [code, setCode] = useState(verifiedPlugin);
  const [rules, setRules] = useState(defaultRules);
  const [ensemble, setEnsemble] = useState<EnsembleDraft[]>([{ strategyVersionId: "", targetWeight: 0.5 }, { strategyVersionId: "", targetWeight: 0.5 }]);
  const [regimes, setRegimes] = useState<RegimeDraft[]>([{ name: "default", strategyVersionId: "", when: '{\n  "kind": "comparison",\n  "left": {"indicator": "rsi", "parameters": {"period": 14}},\n  "comparator": "<",\n  "right": 50\n}' }]);
  const [builtinParameters, setBuiltinParameters] = useState<UnknownRecord>({});
  const [cloneBuiltin, setCloneBuiltin] = useState(false);
  const [form] = Form.useForm<{ name: string; baseStrategyId?: string }>();
  const [versionForm] = Form.useForm<{ message: string }>();
  const versions = useApi<{ items: UnknownRecord[] }>(`versions:${strategyId}`, `/api/v3/strategies/${encodeURIComponent(strategyId)}/versions`, { enabled: Boolean(strategyId) });
  const selectedVersion = useApi<UnknownRecord>(`version:${versionId}`, `/api/v3/strategy-versions/${encodeURIComponent(versionId)}`, { enabled: Boolean(versionId) });
  const versionOptions = useMemo(() => (versions.data?.items ?? []).map((item) => ({ value: String(item.version_id), label: `v${String(item.revision)} · ${String(item.message ?? "未注明")}` })), [versions.data]);
  const selectedBuiltin = contentKind(selectedVersion.data?.content) === "builtin";
  const selectedBuiltinContent = selectedBuiltin ? selectedVersion.data?.content as UnknownRecord : undefined;
  const builtinRegistry = useMemo(() => typeof selectedBuiltinContent?.builtin_identity === "string" ? (strategies.data?.items ?? []).find((item) => item.id === selectedBuiltinContent.builtin_identity) : undefined, [selectedBuiltinContent?.builtin_identity, strategies.data]);
  const validation = useMutation({ mutationFn: (body: UnknownRecord) => post<UnknownRecord>("/api/v3/strategies/validate", body) });
  const create = useMutation({ mutationFn: (body: UnknownRecord) => post<UnknownRecord>("/api/v3/strategies", body), onSuccess: (data) => {
    const draft = data.strategy;
    const initialVersion = data.version;
    if (draft && typeof draft === "object" && typeof (draft as UnknownRecord).strategy_id === "string") setStrategyId((draft as UnknownRecord).strategy_id as string);
    if (initialVersion && typeof initialVersion === "object" && typeof (initialVersion as UnknownRecord).version_id === "string") setVersionId((initialVersion as UnknownRecord).version_id as string);
    void queryClient.invalidateQueries({ queryKey: ["strategies"] });
    setCreateOpen(false);
    message.success("草稿和初始不可变版本已创建。");
  } });
  const saveVersion = useMutation({ mutationFn: (values: { message: string }) => {
    if (!strategyId) return Promise.reject(new Error("Select a draft strategy first."));
    const expectedVersion = selectedVersion.data?.revision;
    if (typeof expectedVersion !== "number") return Promise.reject(new Error("Select an immutable base version first."));
    return post<UnknownRecord>(`/api/v3/strategies/${encodeURIComponent(strategyId)}/versions`, { expected_version: expectedVersion, message: values.message, content: selectedBuiltin ? buildBuiltinContent(selectedBuiltinContent, builtinParameters) : buildStrategyContent(kind, rules, code, ensemble, regimes) });
  }, onSuccess: (version) => {
    // The Lab router returns a StrategyVersion directly, not { version: ... }.
    if (typeof version.version_id === "string") setVersionId(version.version_id);
    void queryClient.invalidateQueries({ queryKey: [`versions:${strategyId}`] });
    message.success("已创建不可变版本。");
  } });
  useEffect(() => {
    const content = selectedVersion.data?.content;
    if (!content || typeof content !== "object") return;
    const source = content as UnknownRecord;
    const loadedKind = contentKind(source);
    if (loadedKind === "builtin") {
      setBuiltinParameters(source.builtin_parameters && typeof source.builtin_parameters === "object" ? source.builtin_parameters as UnknownRecord : {});
      return;
    }
    if (loadedKind === null) return;
    setKind(loadedKind);
    if (loadedKind === "plugin" && typeof source.source_code === "string") { setCode(source.source_code); return; }
    if (loadedKind === "rules") { setRules(JSON.stringify({ entry: source.entry_rule, exit: source.exit_rule }, null, 2)); return; }
    if (loadedKind === "ensemble" && Array.isArray(source.ensemble)) setEnsemble(source.ensemble.flatMap((member) => member && typeof member === "object" && typeof (member as UnknownRecord).strategy_version_id === "string" && typeof (member as UnknownRecord).target_weight === "number" ? [{ strategyVersionId: (member as UnknownRecord).strategy_version_id as string, targetWeight: (member as UnknownRecord).target_weight as number }] : []));
    if (loadedKind === "regime_switch" && Array.isArray(source.regimes)) setRegimes(source.regimes.flatMap((branch) => branch && typeof branch === "object" && typeof (branch as UnknownRecord).name === "string" && typeof (branch as UnknownRecord).strategy_version_id === "string" ? [{ name: (branch as UnknownRecord).name as string, strategyVersionId: (branch as UnknownRecord).strategy_version_id as string, when: JSON.stringify((branch as UnknownRecord).when, null, 2) }] : []));
  }, [selectedVersion.data]);
  const submit = async (): Promise<void> => {
    const values = await form.validateFields();
    try {
      // A builtin clone deliberately has no content: the service copies exact identity and parameters.
      create.mutate(values.baseStrategyId ? { name: values.name, base_strategy_id: values.baseStrategyId } : { name: values.name, ...buildStrategyContent(kind, rules, code, ensemble, regimes) });
    } catch (error) { message.error(error instanceof Error ? error.message : "策略内容必须是有效 JSON AST。"); }
  };
  const validateCurrent = (): void => {
    try { validation.mutate({ content: selectedBuiltin ? buildBuiltinContent(selectedBuiltinContent, builtinParameters) : buildStrategyContent(kind, rules, code, ensemble, regimes) }); }
    catch (error) { message.error(error instanceof Error ? error.message : "策略内容必须是有效 JSON AST。"); }
  };

  return <><PageTitle eyebrow="不可变版本 · 规则组合 · 代码插件" title="策略编辑器"><Paragraph>内置策略仅能精确复制为草稿。规则、插件、加权组合和市场状态分支都保存为不可变版本；浏览器从不执行插件，也不计算正式收益。</Paragraph><Space><Button type="primary" onClick={() => setCreateOpen(true)}>从模板新建草稿</Button><Button onClick={validateCurrent}>校验当前编辑</Button></Space></PageTitle>
    <Card title="策略与版本"><QueryState loading={strategies.isLoading} error={strategies.error} empty={(strategies.data?.items.length ?? 0) === 0}><TableForStrategies rows={strategies.data?.items ?? []} onSelect={(id) => { setStrategyId(id); setVersionId(""); }} /></QueryState>{strategies.error instanceof ApiError && strategies.error.kind === "missing" && <Alert type="warning" showIcon message="策略 API 尚未连接" description="当前服务未提供策略草稿和不可变版本；界面不会生成本地替代版本。" />}</Card>
    {strategyId && <Card title="创建下一策略版本" className="top-gap"><Row gutter={16}><Col xs={24} md={12}><Form.Item label="当前草稿"><Text code>{strategyId}</Text></Form.Item><Form.Item label="不可变基础版本"><Select value={versionId || undefined} onChange={setVersionId} loading={versions.isLoading} options={versionOptions} placeholder="选择基础版本" /></Form.Item></Col><Col xs={24} md={12}><Form form={versionForm} layout="vertical" onFinish={(values) => saveVersion.mutate(values)}><Form.Item name="message" label="本版说明" rules={[{ required: true, message: "说明这次可复现的改动" }]}><Input placeholder="例如：提高 RSI 退出阈值" /></Form.Item><ApiAction label="保存为不可变版本" loading={saveVersion.isPending} disabled={!versionId} onClick={() => versionForm.submit()} /></Form></Col></Row>{selectedBuiltin && <><Alert type="info" showIcon message="内置身份已锁定，可编辑参数" description="保存时将保留 builtin_identity 与 builtin_version，只把参数写入下一不可变版本；规则和算法实现不会被替换。" /><BuiltinParameterEditor registry={builtinRegistry} values={builtinParameters} onChange={setBuiltinParameters} /></>}{selectedVersion.isLoading && <Text>正在载入版本内容…</Text>}{selectedVersion.error && <Alert type="error" showIcon message="版本无法读取" description={apiErrorText(selectedVersion.error)} />}{saveVersion.isError && <Alert type="error" showIcon className="research-notice" message="版本未创建" description={apiErrorText(saveVersion.error)} />}{validation.isError && <Alert type="error" showIcon className="research-notice" message="校验失败" description={apiErrorText(validation.error)} />}{validation.data && <pre className="response-preview">{JSON.stringify(validation.data, null, 2)}</pre>}{!selectedBuiltin && <StrategyContentEditor kind={kind} onKind={setKind} rules={rules} onRules={setRules} code={code} onCode={setCode} ensemble={ensemble} onEnsemble={setEnsemble} regimes={regimes} onRegimes={setRegimes} versionOptions={versionOptions} />}</Card>}
    <Drawer open={createOpen} onClose={() => setCreateOpen(false)} title="创建策略草稿" width={680} extra={<Button type="primary" loading={create.isPending} onClick={() => void submit()}>保存草稿</Button>}><Form form={form} layout="vertical" initialValues={{ name: "", baseStrategyId: undefined }}><Form.Item name="name" label="策略名称" rules={[{ required: true, message: "请输入策略名称" }]}><Input /></Form.Item><Form.Item name="baseStrategyId" label="复制的内置策略"><Select aria-label="选择内置策略" allowClear showSearch optionFilterProp="label" onChange={(value) => setCloneBuiltin(Boolean(value))} options={(strategies.data?.items ?? []).filter((item) => item.read_only === true).map((item) => ({ value: String(item.id ?? item.strategy_id), label: `${String(item.name ?? item.id ?? item.strategy_id)} · 内置只读` }))} /></Form.Item>{cloneBuiltin ? <Alert type="info" showIcon message="将精确复制内置策略" description="创建请求只发送名称和 builtin ID；不会发送当前规则、插件或参数，避免覆盖内置身份。" /> : <StrategyContentEditor kind={kind} onKind={setKind} rules={rules} onRules={setRules} code={code} onCode={setCode} ensemble={ensemble} onEnsemble={setEnsemble} regimes={regimes} onRegimes={setRegimes} versionOptions={versionOptions} />}</Form>{create.isError && <Alert type="error" showIcon message="保存失败" description={apiErrorText(create.error)} />}</Drawer>
  </>;
}

function StrategyContentEditor({ kind, onKind, rules, onRules, code, onCode, ensemble, onEnsemble, regimes, onRegimes, versionOptions }: { kind: StrategyKind; onKind: (kind: StrategyKind) => void; rules: string; onRules: (value: string) => void; code: string; onCode: (value: string) => void; ensemble: EnsembleDraft[]; onEnsemble: (value: EnsembleDraft[]) => void; regimes: RegimeDraft[]; onRegimes: (value: RegimeDraft[]) => void; versionOptions: { value: string; label: string }[] }): ReactElement {
  const updateEnsemble = (index: number, patch: Partial<EnsembleDraft>): void => onEnsemble(ensemble.map((member, memberIndex) => memberIndex === index ? { ...member, ...patch } : member));
  const updateRegime = (index: number, patch: Partial<RegimeDraft>): void => onRegimes(regimes.map((branch, branchIndex) => branchIndex === index ? { ...branch, ...patch } : branch));
  return <div className="top-gap"><Form.Item label="策略形态"><Segmented value={kind} onChange={(value) => onKind(value as StrategyKind)} options={[{ label: "规则树", value: "rules" }, { label: "Python 插件", value: "plugin" }, { label: "加权组合", value: "ensemble" }, { label: "状态分支", value: "regime_switch" }]} /></Form.Item>{kind === "rules" && <Form.Item label="规则 AST"><Input.TextArea aria-label="版本规则 AST" autoSize={{ minRows: 12 }} value={rules} onChange={(event) => onRules(event.target.value)} /></Form.Item>}{kind === "plugin" && <Form.Item label="Python 插件"><Alert type="warning" showIcon className="research-notice" message="受限容器插件 API v1：研究结果尚未验证" description="allocation 参数固定在版本内容中；插件只在 worker 的无网络容器执行。容器隔离不能证明时间点完整性，插件来源的指标不能作为已验证结果或进入已验证排行榜。" /><Suspense fallback={<Text>正在加载代码编辑器…</Text>}><CodeEditor value={code} onChange={onCode} /></Suspense></Form.Item>}{kind === "ensemble" && <CompositionEditor rows={ensemble} options={versionOptions} onChange={onEnsemble} update={updateEnsemble} />}{kind === "regime_switch" && <RegimeEditor regimes={regimes} options={versionOptions} onChange={onRegimes} update={updateRegime} />}</div>;
}

function CompositionEditor({ rows, options, onChange, update }: { rows: EnsembleDraft[]; options: { value: string; label: string }[]; onChange: (rows: EnsembleDraft[]) => void; update: (index: number, patch: Partial<EnsembleDraft>) => void }): ReactElement {
  return <Card size="small" title="加权组合"><Text type="secondary">选择 2–3 个当前草稿的不可变版本；目标权重必须合计 1。</Text>{rows.map((row, index) => <Row gutter={8} className="top-gap" key={index}><Col flex="auto"><Select aria-label={`组合成员 ${index + 1}`} className="full-width" value={row.strategyVersionId || undefined} options={options} onChange={(value) => update(index, { strategyVersionId: value })} placeholder="选择不可变版本" /></Col><Col flex="120px"><InputNumber aria-label={`组合权重 ${index + 1}`} className="full-width" min={0.01} max={1} step={0.05} value={row.targetWeight} onChange={(value) => update(index, { targetWeight: typeof value === "number" ? value : 0 })} /></Col>{rows.length > 2 && <Col><Button onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}>移除</Button></Col>}</Row>)}{rows.length < 3 && <Button className="top-gap" onClick={() => onChange([...rows, { strategyVersionId: "", targetWeight: 0 }])}>添加成员</Button>}</Card>;
}

function RegimeEditor({ regimes, options, onChange, update }: { regimes: RegimeDraft[]; options: { value: string; label: string }[]; onChange: (rows: RegimeDraft[]) => void; update: (index: number, patch: Partial<RegimeDraft>) => void }): ReactElement {
  return <Card size="small" title="状态分支"><Text type="secondary">每个分支有一个可验证的规则 AST 和一个不可变策略版本；执行时后端记录实际选择的分支。</Text>{regimes.map((branch, index) => <Card size="small" className="top-gap" key={index} title={`分支 ${index + 1}`} extra={regimes.length > 1 ? <Button onClick={() => onChange(regimes.filter((_, branchIndex) => branchIndex !== index))}>移除</Button> : undefined}><Row gutter={8}><Col xs={24} md={8}><Input aria-label={`状态名称 ${index + 1}`} value={branch.name} onChange={(event) => update(index, { name: event.target.value })} /></Col><Col xs={24} md={16}><Select aria-label={`状态版本 ${index + 1}`} className="full-width" value={branch.strategyVersionId || undefined} options={options} onChange={(value) => update(index, { strategyVersionId: value })} placeholder="选择不可变版本" /></Col></Row><Input.TextArea aria-label={`状态条件 ${index + 1}`} className="top-gap" autoSize={{ minRows: 5 }} value={branch.when} onChange={(event) => update(index, { when: event.target.value })} /></Card>)}{regimes.length < 3 && <Button className="top-gap" onClick={() => onChange([...regimes, { name: `regime_${regimes.length + 1}`, strategyVersionId: "", when: '{\n  "kind": "comparison",\n  "left": {"indicator": "rsi"},\n  "comparator": ">=",\n  "right": 50\n}' }])}>添加分支</Button>}</Card>;
}

function BuiltinParameterEditor({ registry, values, onChange }: { registry: UnknownRecord | undefined; values: UnknownRecord; onChange: (values: UnknownRecord) => void }): ReactElement {
  const schema = registry?.parameter_schema && typeof registry.parameter_schema === "object" ? registry.parameter_schema as UnknownRecord : undefined;
  const properties = schema?.properties && typeof schema.properties === "object" ? schema.properties as UnknownRecord : {};
  const setValue = (name: string, value: string | number | boolean): void => onChange({ ...values, [name]: value });
  if (Object.keys(properties).length === 0) return <Alert className="research-notice" type="warning" showIcon message="参数定义暂不可用" description="服务端已保留内置身份，但没有返回此内置策略的参数定义；可直接执行原始版本，或稍后重新读取。" />;
  return <Card size="small" className="top-gap" title="内置策略参数"><Row gutter={[12, 0]}>{Object.entries(properties).map(([name, raw]) => <BuiltinParameterField key={name} name={name} schema={raw} value={values[name]} onChange={setValue} />)}</Row></Card>;
}

function BuiltinParameterField({ name, schema, value, onChange }: { name: string; schema: unknown; value: unknown; onChange: (name: string, value: string | number | boolean) => void }): ReactElement {
  const definition = schema && typeof schema === "object" ? schema as UnknownRecord : {};
  const title = typeof definition.title === "string" ? definition.title : name;
  const defaultValue = definition.default;
  const current = value ?? defaultValue;
  const enumValues = Array.isArray(definition.enum) ? definition.enum.filter((item): item is string | number | boolean => typeof item === "string" || typeof item === "number" || typeof item === "boolean") : [];
  const rawType = typeof definition.type === "string" ? definition.type : Array.isArray(definition.anyOf) ? (definition.anyOf.find((item) => item && typeof item === "object" && typeof (item as UnknownRecord).type === "string") as UnknownRecord | undefined)?.type : undefined;
  const minimum = typeof definition.minimum === "number" ? definition.minimum : undefined;
  const maximum = typeof definition.maximum === "number" ? definition.maximum : undefined;
  if (rawType === "boolean") return <Col xs={24} md={12}><Form.Item label={title}><Switch checked={current === true} onChange={(checked) => onChange(name, checked)} /></Form.Item></Col>;
  if (enumValues.length > 0) return <Col xs={24} md={12}><Form.Item label={title}><Select value={current as string | number | boolean | undefined} options={enumValues.map((item) => ({ value: item, label: String(item) }))} onChange={(next) => onChange(name, next as string | number | boolean)} /></Form.Item></Col>;
  if (rawType === "integer" || rawType === "number") return <Col xs={24} md={12}><Form.Item label={title}><InputNumber className="full-width" value={typeof current === "number" ? current : Number(current)} min={minimum} max={maximum} step={rawType === "integer" ? 1 : 0.01} onChange={(next) => { if (typeof next === "number" && Number.isFinite(next)) onChange(name, next); }} /></Form.Item></Col>;
  return <Col xs={24} md={12}><Form.Item label={title}><Input value={typeof current === "string" ? current : String(current ?? "")} onChange={(event) => onChange(name, event.target.value)} /></Form.Item></Col>;
}

function TableForStrategies({ rows, onSelect }: { rows: UnknownRecord[]; onSelect: (strategyId: string) => void }): ReactElement {
  return <Table rowKey={(row) => String(row.strategy_id ?? row.id)} scroll={{ x: 880 }} onRow={(row) => ({ onClick: () => { if (row.read_only !== true && typeof row.strategy_id === "string") onSelect(row.strategy_id); } })} dataSource={rows} columns={[{ title: "策略", render: (_, row) => <Space direction="vertical" size={0}><Text strong>{String(row.name ?? row.identity ?? row.strategy_id ?? row.id ?? "未命名")}</Text><Text type="secondary" className="strategy-id">{String(row.strategy_id ?? row.id ?? "—")}</Text></Space> }, { title: "类型", render: (_, row) => row.read_only === true ? "内置策略" : "研究草稿" }, { title: "版本", render: (_, row) => String(row.version ?? row.revision ?? "—") }, { title: "来源", render: (_, row) => String(row.source ?? row.clone_of ?? "本地草稿") }, { title: "说明", render: (_, row) => String(row.description ?? "—") }, { title: "操作", render: (_, row) => row.read_only === true ? "可复制" : <Button size="small" onClick={(event) => { event.stopPropagation(); if (typeof row.strategy_id === "string") onSelect(row.strategy_id); }}>编辑草稿</Button> }]} />;
}
