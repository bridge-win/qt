import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BtcMonitorPage, type BtcMonitorProjection, type ReceivedBtcMonitorProjection } from "./BtcMonitorPage";

const key = ["btc-monitor", "/api/v3/btc-monitor"];
const clients: QueryClient[] = [];
function fixture(state: "CANDIDATE" | "UNSUITABLE" = "CANDIDATE"): BtcMonitorProjection {
  const now = Date.now() / 1000;
  return {
    read_status: "ok", read_reasons: [], server_time: now, valid_until: now + 5,
    snapshot_age_seconds: 0, snapshot_max_age_seconds: 10,
    snapshot: {
      schema_version: 1, config_version: "test-config", boot_id: "test-session", published_at: now,
      source: { exchange: "binance", product: "spot", symbol: "BTCUSDT", display_symbol: "BTC/USDT", price_type: "trade" },
      health: { alive: true, data_ready: true, assessment_ready: true, status: "ready", reasons: [], coverage_seconds: 60, errors: {}, channels: {
        trade: { last_event_at: now, last_receive_at: now, event_age_seconds: 0, receive_age_seconds: 0, backlog_seconds: 0.1, ready: true, source_time_known: true },
        quote: { last_event_at: null, last_receive_at: now, event_age_seconds: null, receive_age_seconds: 0, backlog_seconds: 0.1, ready: true, source_time_known: false },
      } },
      micro: { price: 60000, spread_bps: 1 }, context: { notifications_configured: false, sensitivity_preset: "balanced", tick_size_assumed: true, tick_size: 0.01, thresholds_calibrated: false }, recent_events: [],
      assessment: { assessment_id: "test-decision", revision: 1, as_of: now, expires_at: now + 5, state, reasons: ["range fixture"],
        gates: [{ name: "net_cost", passed: state === "CANDIDATE", value: 40, threshold: 27, reason: "conservative assumed costs" }],
        evidence: { fee_per_side_bps: 10, slippage_per_side_bps: 2, cost_buffer_bps: 3, round_trip_cost_bps: 27 },
        range_lower: 58000, range_upper: 62000, spacing_bps: 40, net_spacing_bps: 13, levels: 6,
        missing_inputs: ["budget", "saleable_btc_inventory", "actual_fee_tier"],
      },
    },
  };
}
function renderProjection(data: BtcMonitorProjection, retry: boolean | number = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry, retryDelay: 60_000, staleTime: Infinity } } });
  clients.push(client);
  client.setQueryData<ReceivedBtcMonitorProjection>(key, { projection: data, receivedMonotonicMs: performance.now(), requestDurationMs: 0 });
  render(<QueryClientProvider client={client}><BtcMonitorPage /></QueryClientProvider>);
  return client;
}

describe("BTC read-only monitor", () => {
  afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("shows spot identity, cost evidence and missing budget/inventory without execution controls", () => {
    renderProjection(fixture());
    expect(screen.getByText("市场条件候选（仅研究）")).toBeInTheDocument();
    expect(screen.getByText(/binance · spot · BTC\/USDT/)).toBeInTheDocument();
    expect(screen.getByText("trade（现货成交价）")).toBeInTheDocument();
    expect(screen.getByText("balanced（均衡启发式，未实证校准）")).toBeInTheDocument();
    expect(screen.getByText("0.01 USDT（假设，交易所约束未核验）")).toBeInTheDocument();
    expect(screen.getByText("test-config")).toBeInTheDocument();
    expect(screen.getByText(/可用预算 · 可卖出 BTC 库存 · 账户实际费率/)).toBeInTheDocument();
    expect(screen.getByText(/单边手续费 10.00 bps/)).toBeInTheDocument();
    expect(screen.getByText(/40.00 bps \/ 13.00 bps/)).toBeInTheDocument();
    expect(screen.getByText("源无时间戳（使用接收时间）")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /下单|启动交易|启用 live/ })).not.toBeInTheDocument();
  });

  it("renders object-valued motion evidence and robust-range thresholds in full", () => {
    const data = fixture();
    data.snapshot!.assessment.gates.push(
      { name: "two_way_motion", passed: true, value: { reversals: 8, efficiency: 0.18 }, threshold: { min_reversals: 5, max_efficiency: 0.35 }, reason: "observed path" },
      { name: "inside_robust_range", passed: true, value: 60000, threshold: { lower: 58000, upper: 62000 }, reason: "robust bounds" },
    );
    renderProjection(data);
    expect(screen.getByText('{"reversals":8,"efficiency":0.18}')).toBeInTheDocument();
    expect(screen.getByText('{"min_reversals":5,"max_efficiency":0.35}')).toBeInTheDocument();
    expect(screen.getByText('{"lower":58000,"upper":62000}')).toBeInTheDocument();
    expect(screen.queryByText("见指标依据")).not.toBeInTheDocument();
  });

  it("expires a cached candidate using a local timer without a successful poll", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-15T00:00:00Z"));
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const client = renderProjection(fixture());
    expect(screen.getByText("市场条件候选（仅研究）")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(5_250); });
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(screen.getByText("评估暂停 / 历史证据")).toBeInTheDocument();
    expect(client.getQueryData<ReceivedBtcMonitorProjection>(key)?.projection.snapshot?.assessment.state).toBe("CANDIDATE");
  });

  it("expires while a refetch hangs after the client wall clock moves backward", () => {
    vi.useFakeTimers({ toFake: ["Date", "performance", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date("2026-10-15T00:00:00Z"));
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const client = renderProjection(fixture());
    expect(screen.getByText("市场条件候选（仅研究）")).toBeInTheDocument();
    act(() => { void client.invalidateQueries({ queryKey: key }); });
    const receivedAt = client.getQueryData<ReceivedBtcMonitorProjection>(key)!.receivedMonotonicMs;
    vi.setSystemTime(new Date("2026-10-14T23:00:00Z"));
    act(() => { vi.advanceTimersByTime(5_250); });
    expect(performance.now() - receivedAt).toBeGreaterThanOrEqual(5_250);
    expect(client.getQueryState(key)?.fetchStatus).toBe("fetching");
    expect(client.getQueryData<ReceivedBtcMonitorProjection>(key)?.projection.snapshot?.assessment.state).toBe("CANDIDATE");
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(screen.getByText("评估暂停 / 历史证据")).toBeInTheDocument();
  });

  it("does not renew retained cached data when the page remounts", () => {
    vi.useFakeTimers({ toFake: ["Date", "performance", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    const client = renderProjection(fixture());
    expect(screen.getByText("市场条件候选（仅研究）")).toBeInTheDocument();
    cleanup();
    act(() => { vi.advanceTimersByTime(5_250); });
    render(<QueryClientProvider client={client}><BtcMonitorPage /></QueryClientProvider>);
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(screen.getByText("评估暂停 / 历史证据")).toBeInTheDocument();
  });

  it("anchors a genuinely successful replacement response at its monotonic receipt", async () => {
    const replacement = fixture();
    replacement.snapshot!.assessment.revision = 2;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(replacement), { headers: { "content-type": "application/json" } })));
    const client = renderProjection(fixture("UNSUITABLE"));
    const previous = client.getQueryData<ReceivedBtcMonitorProjection>(key)!.receivedMonotonicMs;
    const started = performance.now();
    await act(async () => { await client.invalidateQueries({ queryKey: key }); });
    const received = client.getQueryData<ReceivedBtcMonitorProjection>(key)!;
    expect(received.receivedMonotonicMs).toBeGreaterThanOrEqual(started);
    expect(received.receivedMonotonicMs).toBeGreaterThan(previous);
    expect(received.projection.snapshot?.assessment.revision).toBe(2);
    expect(await screen.findByText("市场条件候选（仅研究）")).toBeInTheDocument();
  });

  it("charges a delayed successful response's full round-trip time against its TTL", async () => {
    vi.useFakeTimers({ toFake: ["Date", "performance", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date("2026-10-15T00:00:00Z"));
    const delayed = fixture();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => {
      window.setTimeout(() => resolve(new Response(JSON.stringify(delayed), { headers: { "content-type": "application/json" } })), 6_000);
    })));
    const client = renderProjection({ ...delayed, read_status: "unavailable", snapshot: null, valid_until: null });
    act(() => { void client.invalidateQueries({ queryKey: key }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
    const received = client.getQueryData<ReceivedBtcMonitorProjection>(key)!;
    expect(received.projection.snapshot?.assessment.state).toBe("CANDIDATE");
    expect(received.requestDurationMs).toBeGreaterThanOrEqual(6_000);
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(screen.getByText("评估暂停 / 历史证据")).toBeInTheDocument();
  });

  it("suppresses a retained positive immediately after an endpoint error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline fixture"); }));
    const data = fixture();
    data.valid_until = data.server_time + 60;
    const client = renderProjection(data);
    expect(screen.getByText("市场条件候选（仅研究）")).toBeInTheDocument();
    await act(async () => { await client.invalidateQueries({ queryKey: key }); });
    await waitFor(() => expect(screen.getByText("监测读取失败，候选已隐藏")).toBeInTheDocument());
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(client.getQueryData<ReceivedBtcMonitorProjection>(key)?.projection.snapshot?.assessment.state).toBe("CANDIDATE");
  });

  it("hides active events and candidates when source health is degraded", () => {
    const data = fixture();
    data.read_status = "degraded";
    data.read_reasons = ["trade_unready"];
    data.snapshot!.health.data_ready = false;
    data.snapshot!.recent_events = [{ event_id: "wick-fixture", revision: 1, state: "CONFIRMED", as_of: data.server_time,
      expires_at: data.server_time + 120, direction: "down", baseline: 60000, extreme: 59000, recovery_fraction: 0.6, reason: "test fixture" }];
    renderProjection(data);
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(screen.queryByText("已确认")).not.toBeInTheDocument();
    expect(screen.getByText("已过期")).toBeInTheDocument();
  });

  it("hides positives on the first failed poll while retries remain pending", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline fixture"); }));
    const client = renderProjection(fixture(), 2);
    act(() => { void client.invalidateQueries({ queryKey: key }); });
    await waitFor(() => expect(screen.getByText("监测读取失败，候选已隐藏")).toBeInTheDocument());
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
    expect(client.getQueryState(key)?.fetchStatus).toBe("fetching");
  });

  it("renders a current noncandidate and its gate evidence", () => {
    renderProjection(fixture("UNSUITABLE"));
    expect(screen.getByText("当前不适合网格")).toBeInTheDocument();
    expect(screen.getByText("未通过")).toBeInTheDocument();
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
  });

  it("renders an invalid-time diagnostic for finite epochs outside the JavaScript Date range", () => {
    const data = fixture();
    data.read_status = "degraded";
    data.read_reasons = ["snapshot_future", "assessment_future"];
    data.snapshot!.published_at = 1e100;
    data.snapshot!.assessment.as_of = 1e100;
    data.snapshot!.assessment.expires_at = 1e100;
    data.snapshot!.health.data_ready = false;
    renderProjection(data);
    expect(screen.getAllByText("无效时间")).toHaveLength(3);
    expect(screen.getByText("当前评估不可用")).toBeInTheDocument();
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
  });

  it("handles first boot with no snapshot", () => {
    renderProjection({ read_status: "unavailable", read_reasons: ["snapshot_missing"], server_time: Date.now() / 1000,
      valid_until: null, snapshot_age_seconds: null, snapshot_max_age_seconds: 10, snapshot: null });
    expect(screen.getByText("监测尚未启动或快照不可用")).toBeInTheDocument();
    expect(screen.queryByText("市场条件候选（仅研究）")).not.toBeInTheDocument();
  });
});
