import { type ReactElement } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Empty, Table, Typography } from "antd";
import { apiErrorText } from "../api";
import { hubRequest, hubToken } from "../hub";
import { PageTitle, type UnknownRecord } from "../workbench";

const { Paragraph } = Typography;

export function HubSchedulesPage(): ReactElement {
  const token = hubToken();
  const schedules = useQuery({
    queryKey: ["hub-schedules", token],
    queryFn: () => hubRequest<{ items: UnknownRecord[] }>("/schedules"),
    enabled: Boolean(token),
    retry: false,
  });
  const items = schedules.data?.items ?? [];
  return (
    <>
      <PageTitle eyebrow="调度语义" title="调度">
        <Paragraph>下次运行同时显示绝对时间、相对时间和时区。overlap / catch-up / notify-on-change 由服务端拥有，通知目的地只读。</Paragraph>
      </PageTitle>
      {schedules.error ? <Alert type="warning" showIcon message="Hub API 尚未连接" description={apiErrorText(schedules.error)} /> : null}
      <Card>
        {items.length === 0 ? <Empty description="没有已启用的调度" /> : (
          <Table
            rowKey={(row) => String(row.id)}
            pagination={false}
            dataSource={items}
            columns={[
              { title: "name", dataIndex: "name" },
              { title: "kind", dataIndex: "kind" },
              { title: "overlap", dataIndex: "overlap_policy" },
              { title: "catchup_s", dataIndex: "catchup_window_s" },
              { title: "下次运行(UTC)", dataIndex: "next_run_at" },
              { title: "相对秒", render: (_, row) => {
                const due = Number(row.next_run_at ?? 0);
                return due ? String(Math.max(0, Math.round(due - Date.now() / 1000))) : "—";
              } },
              { title: "notify_on_change", render: (_, row) => String(row.notify_only_on_change) },
              { title: "enabled", render: (_, row) => String(row.enabled) },
            ]}
          />
        )}
      </Card>
    </>
  );
}
