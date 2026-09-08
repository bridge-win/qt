import { useState, type ChangeEvent, type ReactElement } from "react";
import { Alert, App, Button, Card, Col, Form, Input, Modal, Row, Select, Space, Table } from "antd";
import { useMutation } from "@tanstack/react-query";
import { apiErrorText, post, request } from "../api";
import { QueryState, StatusTag } from "../components";
import type { Dataset, Job, Source } from "../types";
import { PageTitle, useApi } from "../workbench";

const UPLOAD_LIMIT_BYTES = 1_048_576;

type ImportValues = {
  datasetId: string;
  format: "csv" | "parquet";
  objectKey?: string;
};

type SyncValues = {
  source: string;
  datasetId: string;
  symbol: string;
  timeframe: string;
  from: string;
  to: string;
};

export function DataPage(): ReactElement {
  const { message } = App.useApp();
  const datasets = useApi<{ items: Dataset[] }>("datasets", "/api/v3/datasets");
  const sources = useApi<{ items: Source[] }>("sources", "/api/v3/sources");
  const [importOpen, setImportOpen] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false);
  const [importForm] = Form.useForm<ImportValues>();

  const importJob = useMutation({
    mutationFn: (values: ImportValues) => post<{ job: Job }>(
      "/api/v3/imports",
      { source: "staging", dataset_id: values.datasetId, format: values.format, object_key: values.objectKey },
      { "Idempotency-Key": crypto.randomUUID() },
    ),
  });
  const syncJob = useMutation({
    mutationFn: (values: SyncValues) => post<{ job: Job }>(
      "/api/v3/sync-jobs",
      {
        source: values.source,
        dataset_id: values.datasetId,
        symbol: values.symbol,
        timeframe: values.timeframe,
        from: values.from,
        to: values.to,
      },
      { "Idempotency-Key": crypto.randomUUID() },
    ),
  });
  const stageUpload = useMutation({
    mutationFn: (file: File) => request<{ object_key: string; format: "csv" | "parquet"; size_bytes: number }>("/api/v3/staging/uploads", {
      method: "POST",
      headers: uploadHeaders(file),
      body: file,
    }),
  });

  const stageSelectedFile = (event: ChangeEvent<HTMLInputElement>): void => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > UPLOAD_LIMIT_BYTES) {
      message.error("浏览器暂存上传上限为 1 MiB。");
      event.target.value = "";
      return;
    }
    const format = file.name.toLowerCase().endsWith(".csv") ? "csv" : "parquet";
    importForm.setFieldsValue({ format, objectKey: undefined });
    stageUpload.mutate(file, {
      onSuccess: (data) => importForm.setFieldsValue({ objectKey: data.object_key, format: data.format }),
    });
  };

  return (
    <>
      <PageTitle eyebrow="数据资产" title="用覆盖、质量和 available_at 约束研究">
        浏览器只请求分页预览、分段时间窗和元数据；导入、补齐和同步由后端任务执行。不会上传或暴露供应商凭据。
      </PageTitle>
      <Space wrap className="page-actions">
        <Button onClick={() => setImportOpen(true)}>导入 CSV / Parquet</Button>
        <Button onClick={() => setSyncOpen(true)}>请求补齐数据</Button>
      </Space>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={15}>
          <Card title="数据集目录">
            <QueryState loading={datasets.isLoading} error={datasets.error} empty={(datasets.data?.items.length ?? 0) === 0}>
              <Table<Dataset>
                rowKey="dataset_id"
                scroll={{ x: 760 }}
                dataSource={datasets.data?.items}
                columns={[
                  { title: "数据集", dataIndex: "dataset_id" },
                  { title: "状态", dataIndex: "status", render: (value) => <StatusTag value={String(value)} /> },
                  { title: "用途", render: (_, dataset) => <StatusTag value={dataset.standard_ready === true ? "标准历史研究" : nonStandardDatasetLabel(dataset)} /> },
                  { title: "周期", dataIndex: "timeframe", render: (value) => String(value ?? "—") },
                  { title: "行数", dataIndex: "rows", render: (value) => String(value ?? "—") },
                  { title: "来源", dataIndex: "source", render: (value) => String(value ?? "服务未提供") },
                ]}
              />
            </QueryState>
          </Card>
        </Col>
        <Col xs={24} xl={9}>
          <Card title="供应商与订阅状态">
            <QueryState loading={sources.isLoading} error={sources.error} empty={(sources.data?.items.length ?? 0) === 0}>
              <Table<Source>
                size="small"
                rowKey="name"
                pagination={false}
                dataSource={sources.data?.items}
                columns={[
                  { title: "供应商", dataIndex: "name" },
                  { title: "状态", dataIndex: "status", render: (value) => <StatusTag value={String(value)} /> },
                  { title: "范围", dataIndex: "domains", render: (value: string[]) => value.join(" / ") },
                ]}
              />
            </QueryState>
          </Card>
        </Col>
      </Row>
      {syncJob.isError && <Alert type="error" showIcon message="同步未创建" description={apiErrorText(syncJob.error)} />}
      {syncJob.data && <Alert type="success" showIcon message={`同步任务已创建：${syncJob.data.job.job_id}`} />}
      <ImportModal
        form={importForm}
        open={importOpen}
        importJob={importJob}
        stageUpload={stageUpload}
        onClose={() => setImportOpen(false)}
        onSelectFile={stageSelectedFile}
      />
      <SyncModal open={syncOpen} sources={sources.data?.items ?? []} syncJob={syncJob} onClose={() => setSyncOpen(false)} />
    </>
  );
}

function ImportModal({
  form,
  open,
  importJob,
  stageUpload,
  onClose,
  onSelectFile,
}: {
  form: ReturnType<typeof Form.useForm<ImportValues>>[0];
  open: boolean;
  importJob: ReturnType<typeof useMutation<{ job: Job }, Error, ImportValues>>;
  stageUpload: ReturnType<typeof useMutation<{ object_key: string; format: "csv" | "parquet"; size_bytes: number }, Error, File>>;
  onClose: () => void;
  onSelectFile: (event: ChangeEvent<HTMLInputElement>) => void;
}): ReactElement {
  return (
    <Modal open={open} title="导入数据" footer={null} onCancel={onClose}>
      <Form form={form} layout="vertical" onFinish={(values) => importJob.mutate(values)} initialValues={{ format: "parquet" }}>
        <Alert type="info" showIcon message="暂存导入边界" description="只接受不超过 1 MiB 的 CSV 或 Parquet。CSV/Parquet 必须含时间戳列；服务端生成不透明对象键，浏览器不能选择路径。" />
        <Form.Item label="选择文件" required className="top-gap">
          <Input type="file" accept=".csv,.parquet" onChange={onSelectFile} />
        </Form.Item>
        {stageUpload.isError && <Alert type="error" message="文件未暂存" description={apiErrorText(stageUpload.error)} />}
        {stageUpload.data && <Alert type="success" message="文件已安全暂存" description="已取得仅供本次导入使用的服务端对象键。" />}
        <Form.Item name="datasetId" label="目标数据集 ID" rules={[{ required: true }]}>
          <Input placeholder="例如 btcusdt_1h" />
        </Form.Item>
        <Form.Item name="format" label="格式">
          <Select options={[{ value: "csv", label: "CSV" }, { value: "parquet", label: "Parquet" }]} />
        </Form.Item>
        <Form.Item name="objectKey" label="服务端对象键" rules={[{ required: true, message: "请先选择并暂存文件" }]}>
          <Input readOnly />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={importJob.isPending}>创建导入任务</Button>
      </Form>
      {importJob.isError && <Alert type="error" className="research-notice" message="导入未创建" description={apiErrorText(importJob.error)} />}
      {importJob.data && <Alert type="success" className="research-notice" message={`导入任务已创建：${importJob.data.job.job_id}`} description="在任务页轮询共有 job；数据指纹、分区和质量报告仅由 worker 发布。" />}
    </Modal>
  );
}

function SyncModal({
  open,
  sources,
  syncJob,
  onClose,
}: {
  open: boolean;
  sources: Source[];
  syncJob: ReturnType<typeof useMutation<{ job: Job }, Error, SyncValues>>;
  onClose: () => void;
}): ReactElement {
  return (
    <Modal open={open} title="请求数据补齐" footer={null} onCancel={onClose}>
      <Form layout="vertical" onFinish={(values: SyncValues) => syncJob.mutate(values)}>
        <Alert type="info" showIcon message="供应商同步边界" description="使用带时区的 ISO-8601 时间戳（例如 2026-01-01T00:00:00Z），最多 366 天。市场标识支持 BTC/USDT，服务端会安全编码其数据路径。" />
        <Form.Item name="source" label="供应商" rules={[{ required: true }]}>
          <Select options={sources.map((source) => ({ value: source.name, label: `${source.name} · ${source.status}` }))} />
        </Form.Item>
        <Form.Item name="datasetId" label="数据集 ID" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="symbol" label="标的" rules={[{ required: true }]}><Input placeholder="BTC/USDT" /></Form.Item>
        <Form.Item name="timeframe" label="周期" rules={[{ required: true }]}>
          <Select options={["1m", "5m", "15m", "1h", "4h", "8h", "1d"].map((value) => ({ value, label: value }))} />
        </Form.Item>
        <Form.Item name="from" label="开始时间" rules={[{ required: true, pattern: /(?:Z|[+-]\d{2}:\d{2})$/, message: "请输入带时区的 ISO-8601 时间" }]}><Input placeholder="2026-01-01T00:00:00Z" /></Form.Item>
        <Form.Item name="to" label="结束时间" rules={[{ required: true, pattern: /(?:Z|[+-]\d{2}:\d{2})$/, message: "请输入带时区的 ISO-8601 时间" }]}><Input placeholder="2026-01-31T23:00:00Z" /></Form.Item>
        <Button type="primary" htmlType="submit" loading={syncJob.isPending}>创建同步任务</Button>
      </Form>
    </Modal>
  );
}

function uploadHeaders(file: File): HeadersInit {
  const isCsv = file.name.toLowerCase().endsWith(".csv");
  return {
    "Content-Type": isCsv ? "text/csv" : "application/vnd.apache.parquet",
    "X-Upload-Format": isCsv ? "csv" : "parquet",
  };
}

function nonStandardDatasetLabel(dataset: Dataset): string {
  const rows = typeof dataset.rows === "number" ? dataset.rows : 0;
  return rows >= 200 ? "非标准历史数据" : "小样本 / QA 数据";
}
