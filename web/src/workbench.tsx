import type { ReactElement, ReactNode } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Col, Statistic, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { request } from "./api";
import type { Capability, CatalogSignal, StrategyProfile } from "./types";

const { Title, Text } = Typography;

export type UnknownRecord = Record<string, unknown>;
export type CatalogResponse = {
  summary: Record<string, unknown>;
  signals: CatalogSignal[];
  profiles: StrategyProfile[];
};
export type CapabilityResponse = {
  coverage: {
    source_files: Record<string, number>;
    semantic: Record<string, number>;
  };
  items: Capability[];
  semantic_items?: Capability[];
};

export function useApi<T>(key: string, path: string, options?: { enabled?: boolean; refetchInterval?: number }): UseQueryResult<T, Error> {
  return useQuery<T, Error>({ queryKey: [key, path], queryFn: () => request<T>(path), ...options });
}

export function PageTitle({ eyebrow, title, children, action }: { eyebrow: string; title: string; children?: ReactNode; action?: ReactNode }): ReactElement {
  return <div className="page-title"><div><Text className="eyebrow">{eyebrow}</Text><Title level={2}>{title}</Title>{children}</div>{action}</div>;
}

export function Metric({ title, value, suffix }: { title: string; value: string | number; suffix?: string }): ReactElement {
  return <Col xs={24} sm={8}><Statistic title={title} value={value} suffix={suffix} /></Col>;
}

export function genericColumns(preferred: string[]): ColumnsType<UnknownRecord> {
  const keys = preferred.length > 0 ? preferred : ["id", "name", "status", "created_at", "updated_at"];
  return keys.map((key) => ({ title: key, dataIndex: key, render: (value: unknown) => typeof value === "object" && value !== null ? <Text ellipsis={{ tooltip: JSON.stringify(value) }}>{JSON.stringify(value)}</Text> : String(value ?? "—") }));
}

export function parseObject(value: string, label: string): UnknownRecord {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label}必须是非空 JSON 对象。`);
  if (Object.keys(parsed).length === 0) throw new Error(`${label}不能为空。`);
  return parsed as UnknownRecord;
}

export function parseSearchSpace(value: string): Record<string, Array<string | number | boolean>> {
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
