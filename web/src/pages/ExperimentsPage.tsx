import { useState, type ReactElement } from "react";
import { Alert, App, Button, Card, Col, Form, Input, InputNumber, Progress, Row, Select, Space, Table } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiErrorText, post } from "../api";
import { ApiAction, QueryState, ReadOnlyNotice, StatusTag } from "../components";
import type { Dataset, Job } from "../types";
import { PageTitle, type UnknownRecord, useApi } from "../workbench";

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

export function ExperimentsPage(): ReactElement {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const datasets = useApi<{ items: Dataset[] }>("datasets", "/api/v3/datasets");
  const strategies = useApi<{ items: UnknownRecord[] }>("strategies", "/api/v3/strategies?limit=100");
  const [strategyId, setStrategyId] = useState("");
  const versions = useApi<{ items: UnknownRecord[] }>(
    `experiment-versions:${strategyId}`,
    `/api/v3/strategies/${encodeURIComponent(strategyId)}/versions`,
    { enabled: Boolean(strategyId) },
  );
  const jobs = useApi<{ items: Job[] }>("experiments", "/api/v3/experiments?limit=50", { refetchInterval: 5_000 });
  const [form] = Form.useForm<ExperimentFormValues>();
  const submit = useMutation({
    mutationFn: (values: ExperimentFormValues) => submitExperiment(values, datasets.data?.items ?? []),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["experiments"] });
      message.success("研究任务已由服务端接收。");
    },
  });
  const cancel = useMutation({
    mutationFn: (id: string) => post<Job>(`/api/v3/jobs/${id}/cancel`, undefined, { "Idempotency-Key": crypto.randomUUID() }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["experiments"] }),
  });
  const reproduce = useMutation({
    mutationFn: (id: string) => post<ReproductionResponse>(`/api/v3/experiments/${encodeURIComponent(id)}/reproduce`, undefined, { "Idempotency-Key": crypto.randomUUID() }),
    onSuccess: (response) => {
      void queryClient.invalidateQueries({ queryKey: ["experiments"] });
      message.success(response.created ? `已创建复现实验：${response.job.job_id}` : `复现实验已存在：${response.job.job_id}`);
    },
  });

  return (
    <>
      <PageTitle eyebrow="可恢复后台任务" title="回测实验">
        实验只引用已保存的不可变策略版本。提交时记录数据集、窗口、精度、费用与滑点；取消只是请求，最终状态由 common worker 确认。
      </PageTitle>
      <ReadOnlyNotice>正式收益、回撤和成交成本只从完成任务的结果读取。此表单不会将长周期 ticks 拉进浏览器。</ReadOnlyNotice>
      <Card title="创建实验">
        <Alert type="info" showIcon message="数据精度与风险假设" description="K 线研究默认只允许费用；非零点差、滑点、资金费或借贷费需要对应的成交或订单簿数据。现货固定 1x；只有与数据集市场匹配的永续研究可设为 1–3x。这些是研究资本风险假设，不是 live 交易控制。" />
        <Form
          form={form}
          layout="vertical"
          className="top-gap"
          onFinish={(values) => submit.mutate(values)}
          initialValues={{ precision: "bar", market: "spot", validation: "standard", feeBps: 10, spreadBps: 0, slippageBps: 0, fundingBps: 0, leverage: 1, initialCash: 10_000, seed: 7 }}
        >
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.datasetId !== current.datasetId}>
            {({ getFieldValue }) => {
              const dataset = datasets.data?.items.find((item) => item.dataset_id === getFieldValue("datasetId"));
              return dataset?.standard_ready === false ? <Alert className="research-notice" type="warning" showIcon message={datasetReadinessLabel(dataset)} description="该数据尚未标为标准历史研究数据。可用于工作流 QA，但任何结果都不能作为历史收益或投资结论。" /> : null;
            }}
          </Form.Item>
          <ExperimentFields
            datasets={datasets.data?.items ?? []}
            strategies={strategies.data?.items ?? []}
            strategyId={strategyId}
            versions={versions.data?.items ?? []}
            versionsLoading={versions.isLoading}
            onStrategyChange={(value) => {
              setStrategyId(value);
              form.setFieldValue("strategyVersionId", undefined);
            }}
          />
          <ApiAction label="提交研究任务" loading={submit.isPending} onClick={() => form.submit()} />
          {submit.isError && <Alert type="error" showIcon className="research-notice" message="实验未提交" description={apiErrorText(submit.error)} />}
          {submit.data && <Alert type="success" showIcon className="research-notice" message={`任务已接收：${submit.data.job.job_id}`} description="可离开此页面；任务状态会从服务端更新。" />}
        </Form>
      </Card>
      <Card title="持久化实验与任务" className="top-gap">
        <QueryState loading={jobs.isLoading} error={jobs.error} empty={(jobs.data?.items.length ?? 0) === 0}>
          <Table<Job>
            rowKey="job_id"
            dataSource={jobs.data?.items}
            columns={[
              { title: "任务", dataIndex: "job_id" },
              { title: "状态", dataIndex: "status", render: (value) => <StatusTag value={String(value)} /> },
              { title: "阶段", dataIndex: "stage", render: (value) => String(value ?? "—") },
              { title: "进度", dataIndex: "progress", render: progressCell },
              {
                title: "操作",
                render: (_, row) => (
                  <Space size="small">
                    <Button size="small" disabled={!/queued|running|cancelling/.test(row.status)} loading={cancel.isPending} onClick={() => cancel.mutate(row.job_id)}>请求取消</Button>
                    <Button size="small" disabled={row.status !== "succeeded"} loading={reproduce.isPending} onClick={() => reproduce.mutate(row.job_id)}>复现</Button>
                  </Space>
                ),
              },
            ]}
          />
        </QueryState>
        {reproduce.isError && <Alert type="error" showIcon className="research-notice" message="复现实验未创建" description={apiErrorText(reproduce.error)} />}
      </Card>
    </>
  );
}

type ReproductionResponse = { job: Job; created: boolean };

function ExperimentFields({
  datasets,
  strategies,
  strategyId,
  versions,
  versionsLoading,
  onStrategyChange,
}: {
  datasets: Dataset[];
  strategies: UnknownRecord[];
  strategyId: string;
  versions: UnknownRecord[];
  versionsLoading: boolean;
  onStrategyChange: (value: string) => void;
}): ReactElement {
  return (
    <Row gutter={16}>
      <Col xs={24} md={12}>
        <Form.Item label="策略草稿" required>
          <Select showSearch optionFilterProp="label" value={strategyId || undefined} onChange={onStrategyChange} options={strategies.filter((item) => item.read_only !== true).map(strategyOption)} placeholder="先选择一个草稿" />
        </Form.Item>
      </Col>
      <Col xs={24} md={12}>
        <Form.Item name="strategyVersionId" label="不可变策略版本" rules={[{ required: true, message: "请选择已保存的策略版本" }]}>
          <Select loading={versionsLoading} options={versions.map(versionOption)} placeholder="先选择策略草稿" />
        </Form.Item>
      </Col>
      <Col xs={24} md={12}>
        <Form.Item name="datasetId" label="就绪数据集" rules={[{ required: true }]}>
          <Select options={datasets.filter((dataset) => dataset.status === "ready").map((dataset) => ({ value: dataset.dataset_id, label: `${dataset.dataset_id} · ${datasetReadinessLabel(dataset)}` }))} />
        </Form.Item>
      </Col>
      <Col xs={12} md={6}><Form.Item name="market" label="研究市场"><Select options={[{ value: "spot", label: "现货（1x）" }, { value: "perpetual", label: "永续（1–3x）" }]} /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="precision" label="数据精度"><Select options={[{ value: "bar", label: "K 线" }, { value: "trade", label: "成交" }, { value: "order_book", label: "订单簿" }]} /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="validation" label="验证档案"><Select options={[{ value: "quick", label: "快速诊断" }, { value: "standard", label: "标准" }]} /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="leverage" label="研究杠杆"><InputNumber min={1} max={3} step={0.25} className="full-width" /></Form.Item></Col>
      <Col xs={24} md={12}><Form.Item name="from" label="开始时间（可选，必须与结束时间一起填写）"><Input placeholder="2026-01-01T00:00:00Z" /></Form.Item></Col>
      <Col xs={24} md={12}><Form.Item name="to" label="结束时间（可选）"><Input placeholder="2026-02-01T00:00:00Z" /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="initialCash" label="初始资金"><InputNumber min={1} className="full-width" /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="feeBps" label="费用 bps"><InputNumber min={0} className="full-width" /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="spreadBps" label="点差 bps"><InputNumber min={0} className="full-width" /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="slippageBps" label="滑点 bps"><InputNumber min={0} className="full-width" /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="fundingBps" label="资金费 bps"><InputNumber min={0} className="full-width" /></Form.Item></Col>
      <Col xs={12} md={6}><Form.Item name="seed" label="随机种子"><InputNumber min={0} className="full-width" /></Form.Item></Col>
    </Row>
  );
}

function submitExperiment(values: ExperimentFormValues, datasets: Dataset[]): Promise<{ job: Job }> {
  const dataset = datasets.find((item) => item.dataset_id === values.datasetId);
  if (!dataset || dataset.status !== "ready") return Promise.reject(new Error("Select a ready server-provided dataset first."));
  if ((values.from && !values.to) || (!values.from && values.to)) return Promise.reject(new Error("The experiment window needs both from and to timestamps."));
  if (values.precision === "bar" && (values.spreadBps > 0 || values.slippageBps > 0 || values.fundingBps > 0)) return Promise.reject(new Error("Bar-only OHLCV cannot support spread, slippage, or funding assumptions."));
  if (values.market === "spot" && values.leverage !== 1) return Promise.reject(new Error("Spot research uses 1x leverage; choose a matching perpetual dataset for 1–3x research leverage."));
  return post<{ job: Job }>("/api/v3/experiments", {
    strategy_version_id: values.strategyVersionId,
    dataset_id: dataset.dataset_id,
    precision: values.precision,
    validation: values.validation,
    seed: values.seed,
    market: values.market,
    ...(values.from ? { from: values.from, to: values.to } : {}),
    costs: {
      initial_cash: values.initialCash,
      fee_bps: values.feeBps,
      spread_bps: values.spreadBps,
      slippage_bps: values.slippageBps,
      funding_bps: values.fundingBps,
      leverage: values.leverage,
    },
  }, { "Idempotency-Key": crypto.randomUUID() });
}

function strategyOption(item: UnknownRecord): { value: string; label: string } {
  return { value: String(item.strategy_id), label: String(item.name ?? item.strategy_id) };
}

function versionOption(item: UnknownRecord): { value: string; label: string } {
  return { value: String(item.version_id), label: `v${String(item.revision)} · ${String(item.message ?? "未注明")}` };
}

function datasetReadinessLabel(dataset: Dataset): string {
  if (dataset.standard_ready === true) return "标准历史研究";
  return typeof dataset.rows === "number" && dataset.rows >= 200 ? "非标准历史数据" : "小样本 / QA 数据";
}

function progressCell(value: unknown): ReactElement | string {
  return typeof value === "number" ? <Progress percent={Math.min(100, Math.max(0, Math.round(value)))} size="small" /> : "—";
}
