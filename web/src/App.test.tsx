import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import WorkbenchApp from "./App";

function json(body: unknown): Response { return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }); }

describe("workbench workflow", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("navigates from the evidence overview to a real strategy-draft workflow", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      if (path.includes("/runtime")) return json({ mode: "research", live_enabled: false, available_memory_mib: 512, worker: { online: true } });
      if (path.includes("/capabilities")) return json({ coverage: { source_files: { total: 2, pending_behavior_verification: 2 }, semantic: { pending_adapter: 2 } }, items: [], semantic_items: [] });
      if (path.includes("/catalog")) return json({ summary: { signal_count: 50, profile_count: 100, sha256: "catalog-proof" }, signals: [], profiles: [] });
      if (path.includes("/datasets")) return json({ items: [] });
      if (path.includes("/results")) return json({ items: [] });
      if (path.includes("/strategies")) return json({ items: [{ id: "sma_crossover", name: "SMA crossover", read_only: true }] });
      return json({ items: [] });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><WorkbenchApp /></QueryClientProvider>);
    expect(await screen.findByText("从证据开始，而不是从曲线开始")).toBeInTheDocument();
    await userEvent.click(screen.getByText("策略与规则"));
    expect(await screen.findByText("策略编辑器")).toBeInTheDocument();
    await userEvent.click(screen.getByText("从模板新建草稿"));
    expect(await screen.findByLabelText("策略名称")).toBeInTheDocument();
    expect(screen.getByText("保存草稿")).toBeInTheDocument();
  }, 10_000);

  it("creates an immutable draft and explicitly saves its next version", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/runtime")) return json({ mode: "research", live_enabled: false, available_memory_mib: 512, worker: { online: true } });
      if (path.includes("/capabilities")) return json({ coverage: { source_files: { total: 2, pending_behavior_verification: 2 }, semantic: { pending_adapter: 2 } }, items: [] });
      if (path.includes("/catalog")) return json({ summary: {}, signals: [], profiles: [] });
      if (path.includes("/datasets") || path.includes("/results")) return json({ items: [] });
      if (path.includes("/strategy-versions/v1")) return json({ version_id: "v1", revision: 1, content: { mode: "rules", entry_rule: { kind: "comparison" }, exit_rule: { kind: "comparison" } } });
      if (path.includes("/strategy-versions/v2")) return json({ version_id: "v2", revision: 2, content: { mode: "rules", entry_rule: { kind: "comparison" }, exit_rule: { kind: "comparison" } } });
      if (path.includes("/strategies/draft-1/versions") && init?.method === "POST") {
        const request = JSON.parse(String(init.body)) as { expected_version: number };
        return json({ version_id: request.expected_version === 1 ? "v2" : "v3", revision: request.expected_version + 1 });
      }
      if (path.includes("/strategies/draft-1/versions")) return json({ items: [{ version_id: "v1", revision: 1, message: "initial" }] });
      if (path.endsWith("/strategies") && init?.method === "POST") return json({ strategy: { strategy_id: "draft-1" }, version: { version_id: "v1" } });
      if (path.includes("/strategies")) return json({ items: [] });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><WorkbenchApp /></QueryClientProvider>);
    await userEvent.click(await screen.findByText("策略与规则"));
    await userEvent.click(await screen.findByText("从模板新建草稿"));
    await userEvent.type(screen.getByLabelText("策略名称"), "Versioned rules");
    await userEvent.click(screen.getByText("保存草稿"));
    const messageField = await screen.findByLabelText("本版说明");
    await userEvent.type(messageField, "Tighten exit");
    await userEvent.click(screen.getByText("保存为不可变版本"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/strategies/draft-1/versions"), expect.objectContaining({ method: "POST" })));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/strategy-versions/v2"), expect.anything()));
    await userEvent.clear(messageField);
    await userEvent.type(messageField, "Second immutable edit");
    await userEvent.click(screen.getByText("保存为不可变版本"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/strategies/draft-1/versions"), expect.objectContaining({ body: expect.stringContaining('"expected_version":2') })));
  }, 15_000);

  it("clones a builtin without submitting editor content", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (path.includes("/runtime")) return json({ mode: "research", live_enabled: false, available_memory_mib: 512, worker: { online: true } });
      if (path.includes("/capabilities")) return json({ coverage: { source_files: {}, semantic: {} }, items: [] });
      if (path.includes("/catalog")) return json({ summary: {}, signals: [], profiles: [] });
      if (path.includes("/datasets") || path.includes("/results")) return json({ items: [] });
      if (path.endsWith("/strategies") && init?.method === "POST") return json({ strategy: { strategy_id: "builtin-copy" }, version: { version_id: "builtin-v1" } });
      if (path.includes("/strategies")) return json({ items: [{ id: "qt:buy_and_hold", name: "Buy and hold", read_only: true }] });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><WorkbenchApp /></QueryClientProvider>);
    await userEvent.click(await screen.findByText("策略与规则"));
    await userEvent.click(await screen.findByText("从模板新建草稿"));
    await userEvent.type(screen.getByLabelText("策略名称"), "Exact builtin copy");
    await userEvent.click(screen.getByRole("combobox", { name: "选择内置策略" }));
    await userEvent.click(await screen.findByText("Buy and hold · 内置只读"));
    await userEvent.click(screen.getByText("保存草稿"));
    await waitFor(() => {
      const request = fetchMock.mock.calls.find(([path, init]) => String(path).endsWith("/strategies") && (init as RequestInit | undefined)?.method === "POST");
      expect(request).toBeDefined();
      expect(JSON.parse(String((request?.[1] as RequestInit).body))).toEqual({ name: "Exact builtin copy", base_strategy_id: "qt:buy_and_hold" });
    });
  }, 15_000);

  it("renders server learning semantics instead of an unavailable-document fallback", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      if (path.includes("/runtime")) return json({ mode: "research", live_enabled: false, available_memory_mib: 512, worker: { online: true } });
      if (path.includes("/capabilities")) return json({ coverage: { source_files: {}, semantic: {} }, items: [] });
      if (path.includes("/learning-docs")) return json({ items: [{
        id: "ema_cross_up",
        formula_zh: "EMA(收盘价, fast_period) 向上穿越 EMA(收盘价, slow_period)",
        condition_zh: "快 EMA 向上穿越慢 EMA",
        effective_parameters: ["fast_period", "slow_period"],
        ignored_legacy_parameters: ["legacy_window"],
        cross_semantics: "current left > right and previous left <= right",
        warmup: "Use completed bars only.",
        available_at: "derived after the completed market bar",
        caveats: ["No provider timestamp is implied."],
        parameters: [{ name: "fast_period", default: 12, minimum: 3, maximum: 120 }],
      }], next_cursor: null });
      if (path.includes("/catalog")) return json({ summary: {}, signals: [{ id: "ema_cross_up", category: "trend", direction: "entry", evaluator: "ema_cross_up", required_indicators: ["ema_fast", "ema_slow"], parameters: [] }], profiles: [] });
      if (path.includes("/indicators")) return json({ items: [] });
      return json({ items: [] });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><WorkbenchApp /></QueryClientProvider>);
    await userEvent.click(await screen.findByText("学习与指标"));
    await userEvent.click((await screen.findAllByText("ema_cross_up"))[0]);
    expect(await screen.findByText("EMA(收盘价, fast_period) 向上穿越 EMA(收盘价, slow_period)")).toBeInTheDocument();
    expect(screen.getByText("快 EMA 向上穿越慢 EMA")).toBeInTheDocument();
    expect(screen.getByText("legacy_window")).toBeInTheDocument();
    expect(screen.getByText("No provider timestamp is implied.")).toBeInTheDocument();
  }, 15_000);
});
