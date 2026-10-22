import { useState, type ReactElement } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Form, Input, Space, Table, Tag, Typography } from "antd";
import { Link } from "react-router-dom";
import { apiErrorText } from "../api";
import { hubPost, hubRequest, hubToken, setHubToken } from "../hub";
import { PageTitle, type UnknownRecord } from "../workbench";

const { Paragraph, Text } = Typography;

type Inbox = {
  approvals: UnknownRecord[];
  failed_jobs: UnknownRecord[];
  next_run_at: number | null;
  next_run_in_seconds: number | null;
  timezone: string;
  empty_reason: string | null;
};

export function HubInboxPage(): ReactElement {
  const client = useQueryClient();
  const [token, setToken] = useState(hubToken());
  const inbox = useQuery({
    queryKey: ["hub-inbox", token],
    queryFn: () => hubRequest<Inbox>("/inbox"),
    enabled: Boolean(token),
    retry: false,
  });
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "once" | "class" | "reject" }) =>
      hubPost(`/approvals/${id}`, { decision }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["hub-inbox"] }),
  });
  const approvals = inbox.data?.approvals ?? [];
  const failed = inbox.data?.failed_jobs ?? [];
  return (
    <>
      <PageTitle eyebrow="PAPER 默认 · 证据优先" title="收件箱">
        <Paragraph>需要你处理的审批、失败运行和下一次调度。空态会写明下次运行时间，不会把「多个 Agent 一致」当成质量信号。</Paragraph>
      </PageTitle>
      <Card>
        <Form layout="inline" onFinish={() => { setHubToken(token); client.invalidateQueries({ queryKey: ["hub-inbox"] }); }}>
          <Form.Item label="Hub token"><Input.Password aria-label="Hub token" value={token} onChange={(event) => setToken(event.target.value)} /></Form.Item>
          <Button htmlType="submit">连接 Agent Hub</Button>
        </Form>
      </Card>
      {inbox.error ? <Alert className="top-gap" type="warning" showIcon message="Hub API 尚未连接" description={apiErrorText(inbox.error)} /> : null}
      {inbox.data && approvals.length === 0 && failed.length === 0 ? (
        <Empty className="top-gap" description={inbox.data.empty_reason ?? `下次运行 ${inbox.data.next_run_in_seconds ?? "—"} 秒后 · ${inbox.data.timezone}`} />
      ) : null}
      <Card className="top-gap" title="待审批">
        <Table
          rowKey={(row) => String(row.id)}
          pagination={false}
          dataSource={approvals}
          columns={[
            { title: "动作", dataIndex: "risk_tier" },
            { title: "倒计时", render: (_, row) => {
              const remaining = Number(row.expires_at ?? 0) - Date.now() / 1000;
              return remaining > 0 ? `${Math.round(remaining)}s` : "已过期";
            } },
            { title: "run", render: (_, row) => <Link to={`/hub/runs/${String(row.run_id)}`}>{String(row.run_id)}</Link> },
            { title: "处理", render: (_, row) => (
              <Space>
                <Button size="small" type="primary" onClick={() => decide.mutate({ id: String(row.id), decision: "once" })}>once</Button>
                <Button size="small" onClick={() => decide.mutate({ id: String(row.id), decision: "class" })}>class</Button>
                <Button size="small" danger onClick={() => decide.mutate({ id: String(row.id), decision: "reject" })}>reject</Button>
              </Space>
            ) },
          ]}
        />
      </Card>
      <Card className="top-gap" title="失败运行">
        <Table
          rowKey={(row) => String(row.id)}
          pagination={false}
          dataSource={failed}
          columns={[
            { title: "job", dataIndex: "id", render: (value) => <Link to={`/hub/runs/${String(value)}`}>{String(value)}</Link> },
            { title: "kind", dataIndex: "kind" },
            { title: "error", dataIndex: "error" },
            { title: "status", render: (_, row) => <Tag color="red">{String(row.status)}</Tag> },
          ]}
        />
      </Card>
      <Text type="secondary">调度绝对时间 {inbox.data?.next_run_at ?? "—"} · 相对 {inbox.data?.next_run_in_seconds ?? "—"}s · 时区 {inbox.data?.timezone ?? "UTC"}</Text>
    </>
  );
}
