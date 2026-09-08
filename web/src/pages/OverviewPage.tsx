import type { ReactElement } from "react";
import { Button, Card, Col, Descriptions, Row, Table, Typography } from "antd";
import { Link } from "react-router-dom";
import { QueryState, ReadOnlyNotice } from "../components";
import type { Dataset, Runtime } from "../types";
import { genericColumns, Metric, PageTitle, type CapabilityResponse, type CatalogResponse, type UnknownRecord, useApi } from "../workbench";

const { Paragraph } = Typography;

export function OverviewPage(): ReactElement {
  const capabilities = useApi<CapabilityResponse>("capabilities", "/api/v3/capabilities");
  const catalog = useApi<CatalogResponse>("catalog", "/api/v3/catalog");
  const datasets = useApi<{ items: Dataset[] }>("datasets", "/api/v3/datasets");
  const runtime = useApi<Runtime>("runtime", "/api/v3/runtime", { refetchInterval: 30_000 });
  const results = useApi<{ items: UnknownRecord[] }>("results", "/api/v3/results?limit=8");
  const sourceCoverage = capabilities.data?.coverage?.source_files;
  const semanticCoverage = capabilities.data?.coverage?.semantic;

  return (
    <>
      <PageTitle eyebrow="研究控制面" title="从证据开始，而不是从曲线开始" action={<Link to="/experiments"><Button type="primary">新建真实实验</Button></Link>}>
        <Paragraph>所有指标、信号、收益和解释都由受保护的 <code>/api/v3</code> 服务提供。前端不计算正式回报，也不把迁移来源当作已验证结论。</Paragraph>
      </PageTitle>
      <ReadOnlyNotice>默认模式为 research。提交实验会创建可恢复的服务端任务；关闭浏览器不会取消任务，实盘开关不会从网页自动打开。</ReadOnlyNotice>
      <QueryState loading={runtime.isLoading} error={runtime.error}>
        <Row gutter={[16, 16]} className="metric-row">
          <Metric title="运行模式" value={runtime.data?.mode ?? "未知"} />
          <Metric title="可用内存" value={runtime.data?.available_memory_mib ?? "未知"} suffix={runtime.data?.available_memory_mib === null ? "" : "MiB"} />
          <Metric title="worker" value={runtime.data?.worker.online ? "在线" : "未知"} />
        </Row>
      </QueryState>
      <QueryState loading={capabilities.isLoading} error={capabilities.error}>
        <Row gutter={[16, 16]} className="metric-row">
          <Metric title="保留源文件" value={sourceCoverage?.total ?? "—"} />
          <Metric title="源文件待行为验证" value={sourceCoverage?.pending_behavior_verification ?? "—"} />
          <Metric title="语义能力待适配" value={semanticCoverage?.pending_adapter ?? "—"} />
        </Row>
      </QueryState>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={14}>
          <Card title="最近实验结果" extra={<Link to="/experiments">全部任务</Link>}>
            <QueryState loading={results.isLoading} error={results.error} empty={(results.data?.items.length ?? 0) === 0}>
              <Table size="small" rowKey={(row) => String(row.run_id ?? row.job_id ?? JSON.stringify(row))} pagination={false} dataSource={results.data?.items} columns={genericColumns(["run_id", "job_id", "status", "created_at", "completed_at"])} />
            </QueryState>
          </Card>
        </Col>
        <Col xs={24} xl={10}>
          <Card title="目录覆盖" extra={<Link to="/learn">打开学习中心</Link>}>
            <QueryState loading={catalog.isLoading} error={catalog.error}>
              <Descriptions size="small" column={1}>
                <Descriptions.Item label="信号">{String(catalog.data?.summary.signal_count ?? "未知")}</Descriptions.Item>
                <Descriptions.Item label="策略配置">{String(catalog.data?.summary.profile_count ?? "未知")}</Descriptions.Item>
                <Descriptions.Item label="版本化目录校验">{String(catalog.data?.summary.sha256 ?? "服务未提供")}</Descriptions.Item>
              </Descriptions>
            </QueryState>
            <QueryState loading={datasets.isLoading} error={datasets.error}>
              <Paragraph className="subtle">就绪数据集：{datasets.data?.items.filter((item) => item.status === "ready").length ?? 0}。大范围分钟/逐笔请求必须先通过服务端容量策略，不会完整倾倒到浏览器。</Paragraph>
            </QueryState>
          </Card>
        </Col>
      </Row>
    </>
  );
}
