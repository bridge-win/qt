import { type ReactElement } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, Col, Descriptions, Empty, Row, Tabs, Tag, Timeline, Typography } from "antd";
import { useParams } from "react-router-dom";
import { apiErrorText } from "../api";
import { hubRequest } from "../hub";
import { PageTitle, type UnknownRecord } from "../workbench";

const { Paragraph, Text } = Typography;

type RunResponse = { run_id: string; items: UnknownRecord[]; truncated: boolean };

export function HubRunPage(): ReactElement {
  const { runId = "" } = useParams();
  const run = useQuery({
    queryKey: ["hub-run", runId],
    queryFn: () => hubRequest<RunResponse>(`/runs/${encodeURIComponent(runId)}`),
    enabled: Boolean(runId),
    retry: false,
  });
  const diagnostics = useQuery({
    queryKey: ["hub-run-diagnostics", runId],
    queryFn: () => hubRequest<UnknownRecord>(`/runs/${encodeURIComponent(runId)}/diagnostics`),
    enabled: Boolean(runId),
    retry: false,
  });
  const steps = run.data?.items ?? [];
  const latest = steps.at(-1);
  const result = (latest?.result && typeof latest.result === "object" ? latest.result : {}) as UnknownRecord;
  const proposal = (result.proposal && typeof result.proposal === "object" ? result.proposal : result) as UnknownRecord;
  const validation = (result.evidence_validation && typeof result.evidence_validation === "object"
    ? result.evidence_validation
    : {}) as UnknownRecord;
  return (
    <>
      <PageTitle eyebrow="运行详情" title={runId || "未选择运行"}>
        <Paragraph>左栏阶段时间线，中栏报告，右栏证据 / 计划 / 预算 / 验证。证据优先于置信度。</Paragraph>
      </PageTitle>
      {run.error ? <Card>{apiErrorText(run.error)}</Card> : null}
      {!runId ? <Empty description="从收件箱打开一次运行" /> : (
        <Row gutter={16}>
          <Col xs={24} lg={6}>
            <Card title="ThoughtChain">
              <Timeline
                items={steps.map((step) => ({
                  color: step.status === "deadletter" ? "red" : step.status === "running" ? "orange" : "green",
                  children: (
                    <div>
                      <strong>{String(step.kind)}</strong>
                      <div><Tag>{String(step.status)}</Tag></div>
                      <Text type="secondary">{String((step.result as UnknownRecord | undefined)?.reason ?? step.error ?? "")}</Text>
                    </div>
                  ),
                }))}
              />
            </Card>
          </Col>
          <Col xs={24} lg={10}>
            <Card title="报告">
              <pre className="response-preview">{JSON.stringify(result.answer ?? result, null, 2)}</pre>
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Tabs
              items={[
                {
                  key: "evidence",
                  label: "证据",
                  children: (
                    <Descriptions bordered size="small" column={1}>
                      <Descriptions.Item label="receipt">{String(proposal.evidence_receipt ?? "—")}</Descriptions.Item>
                      <Descriptions.Item label="hash">{String(proposal.evidence_hash ?? "—")}</Descriptions.Item>
                      <Descriptions.Item label="as-of">{JSON.stringify(proposal.time_boundary ?? "—")}</Descriptions.Item>
                      <Descriptions.Item label="valid">{String(validation.valid ?? "—")}</Descriptions.Item>
                    </Descriptions>
                  ),
                },
                {
                  key: "plan",
                  label: "计划",
                  children: <pre className="response-preview">{JSON.stringify(result.planner ?? proposal, null, 2)}</pre>,
                },
                {
                  key: "validation",
                  label: "验证",
                  children: <pre className="response-preview">{JSON.stringify(diagnostics.data ?? result.experiment ?? {}, null, 2)}</pre>,
                },
              ]}
            />
          </Col>
        </Row>
      )}
    </>
  );
}
