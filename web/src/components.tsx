import type { ReactElement, ReactNode } from "react";
import { Alert, Button, Empty, Space, Spin, Tag } from "antd";
import type { ApiError } from "./api";
import { apiErrorText } from "./api";

export function QueryState({ children, loading, error, empty }: { children: ReactNode; loading: boolean; error: unknown; empty?: boolean }): ReactElement {
  if (loading) return <div className="query-state"><Space><Spin /><span>正在读取研究服务…</span></Space></div>;
  if (error) {
    const apiError = error as ApiError;
    const isMissing = apiError.kind === "missing";
    return <Alert className="query-state-error" type={isMissing ? "warning" : "error"} showIcon message={isMissing ? "本区功能暂未连接" : "本区暂时不可用"} description={<Space wrap><span>{apiErrorText(error)}</span><Button size="small" onClick={() => window.location.reload()}>重新读取</Button></Space>} />;
  }
  if (empty) return <Empty description="服务没有返回可展示的记录" />;
  return <>{children}</>;
}

export function ReadOnlyNotice({ children }: { children: ReactNode }): ReactElement {
  return <Alert type="info" showIcon message="研究模式" description={children} className="research-notice" />;
}

export function StatusTag({ value }: { value: string | boolean | null | undefined }): ReactElement {
  const text = value === null || value === undefined ? "未知" : String(value);
  const color = /verified|ready|available|succeeded|online|research/i.test(text)
    ? "success"
    : /failed|cancelled|blocked|offline|needs|not_run|unverified|interrupted/i.test(text)
      ? "warning"
      : "default";
  return <Tag color={color}>{text}</Tag>;
}

export function ApiAction({ label, onClick, loading, disabled }: { label: string; onClick: () => void; loading?: boolean; disabled?: boolean }): ReactElement {
  return <Button type="primary" onClick={onClick} loading={loading} disabled={disabled}>{label}</Button>;
}
