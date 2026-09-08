export interface Capability {
  id: string;
  category: string;
  source_repository: string;
  source_commit: string;
  source_path: string;
  source_sha256: string;
  target_path: string;
  entry_point: string;
  migration_status: string;
  data_validation_status: string;
  notes: string;
}

export interface CatalogSignal {
  id: string;
  name?: string;
  family?: string;
  description?: string;
  formula?: string;
  [key: string]: unknown;
}

export interface StrategyProfile {
  id: string;
  family: string;
  pair: string;
  timeframe: string;
  version: number;
  entry_signals: string[];
  exit_signals: string[];
  risk: Record<string, number>;
  signal_params: Record<string, Record<string, number>>;
}

export interface Dataset {
  dataset_id: string;
  status: string;
  rows?: number;
  timeframe?: string;
  symbol?: string;
  coverage?: string;
  [key: string]: unknown;
}

export interface Source {
  name: string;
  tier: string;
  domains: string[];
  credential_env: string[];
  entry_point: string;
  status: string;
}

export interface Job {
  job_id: string;
  status: "queued" | "running" | "cancelling" | "cancelled" | "succeeded" | "failed" | "interrupted" | string;
  created_at?: string;
  updated_at?: string;
  stage?: string;
  progress?: number;
  error?: string;
  [key: string]: unknown;
}

export interface Runtime {
  mode: string;
  live_enabled: boolean;
  available_memory_mib: number | null;
  worker: { online: boolean; [key: string]: unknown };
}
