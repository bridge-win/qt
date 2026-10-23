import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ProductionReadinessPage } from "./ProductionReadinessPage";

afterEach(() => vi.unstubAllGlobals());

it("shows missing evidence and does not offer a live switch", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
    schema_version: "2.0", live_ready: false,
    engineering: { status: "unverified", experiments: [{ experiment_hash: "frozen", stage: "paper", eligible: false, checks: [{ name: "causality_verified", status: "insufficient", observed: null, threshold: 1, method: "future mutation", reason: "missing" }] }] },
    research: { status: "unverified" }, strategy: { status: "unverified" }, economics: { status: "unverified" },
  }), { status: 200, headers: { "Content-Type": "application/json" } })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ProductionReadinessPage /></QueryClientProvider>);
  expect(await screen.findByText("实盘准入尚未通过")).toBeInTheDocument();
  expect(screen.getByText("证据不足")).toBeInTheDocument();
  expect(screen.getByText("未知")).toBeInTheDocument();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /启用.*live/i })).not.toBeInTheDocument();
  client.clear();
});
