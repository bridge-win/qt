import { Alert, Card, Col, Descriptions, Empty, Row, Table, Tag, Typography } from "antd";
import { PageTitle, useApi } from "../workbench";
import { QueryState } from "../components";

type Check = { name: string; status: string; observed: number | boolean | null; threshold: number; method: string; reason: string };
type Verdict = { experiment_hash: string; eligible: boolean; stage: string; checks: Check[] };
type Dimension = { status: string; reason?: string; experiments?: Verdict[] };
type Readiness = {
  schema_version: string; live_ready: boolean; engineering: Dimension; research: Dimension;
  strategy: Dimension; economics: Dimension; browser_acceptance: string;
};

const labels = { engineering: "工程可靠", research: "研究有效", strategy: "策略有效", economics: "经济可行" } as const;
const reasons: Record<string, string> = {
  experiment_integrity_only_operational_soak_and_recovery_acceptance_required: "实验完整性已有检查；持续运行和恢复验收仍需补齐。",
  frozen_agent_ablation_dataset_required: "需用冻结任务集比较无 Agent、单 Agent 和多 Agent 的效果与成本。",
  actual_cost_and_forward_profit_evidence_required: "需补齐真实成本和前向净利润证据。",
};

export function ProductionReadinessPage() {
  const query = useApi<Readiness>("production-readiness", "/api/v3/production/readiness", { refetchInterval: 30_000 });
  return <>
    <PageTitle eyebrow="证据与准入" title="生产验收">
      <Typography.Paragraph>工程、研究、策略与经济性分别验收。缺少样本、前向观察或真实成交证据时保持未验证；门禁阈值不代表收益承诺。</Typography.Paragraph>
    </PageTitle>
    <QueryState loading={query.isLoading} error={query.error}>
      {query.data && <>
        <Alert showIcon type={query.data.live_ready ? "success" : "warning"} message={query.data.live_ready ? "已有实验通过实盘证据门禁" : "实盘准入尚未通过"} description="资金交易还需绑定签名部署、账户、模型、策略、费用和风险版本。此页面不提供实盘开关。" />
        <Row gutter={[16, 16]} className="top-gap">
          {Object.entries(labels).map(([key, label]) => {
            const dimension = query.data![key as keyof typeof labels];
            return <Col xs={24} xl={12} key={key}><Card title={label} extra={<Tag color={dimension.status === "verified" ? "green" : "gold"}>{dimension.status === "verified" ? "已验证" : dimension.status === "partial" ? "部分验证" : "未验证"}</Tag>}>
              {dimension.reason && <Typography.Paragraph type="secondary">{reasons[dimension.reason] ?? "验收证据尚未齐备。"}</Typography.Paragraph>}
              {(dimension.experiments ?? []).length === 0 ? <Empty description="尚无对应的验收证据" /> : dimension.experiments!.map((verdict) => <section key={`${verdict.experiment_hash}:${verdict.stage}`}>
                <Descriptions column={1} size="small"><Descriptions.Item label="实验"><Typography.Text copyable>{verdict.experiment_hash}</Typography.Text></Descriptions.Item><Descriptions.Item label="阶段">{verdict.stage} · {verdict.eligible ? "通过" : "阻断"}</Descriptions.Item></Descriptions>
                <Table<Check> rowKey="name" size="small" pagination={{ pageSize: 8 }} scroll={{ x: 560 }} dataSource={verdict.checks} columns={[
                  { title: "检查", dataIndex: "name" },
                  { title: "状态", dataIndex: "status", render: (status: string) => <Tag color={status === "pass" ? "green" : status === "fail" ? "red" : "gold"}>{status === "insufficient" ? "证据不足" : status}</Tag> },
                  { title: "观测值", dataIndex: "observed", render: (value: Check["observed"]) => value === null ? "未知" : String(value) },
                  { title: "阈值", dataIndex: "threshold" },
                  { title: "方法", dataIndex: "method" },
                ]} />
              </section>)}
            </Card></Col>;
          })}
        </Row>
      </>}
    </QueryState>
  </>;
}
