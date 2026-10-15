import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PolymarketSnapshotCard, polymarketQueryKey, type PolymarketSnapshot, type ReceivedPolymarketSnapshot } from "./PolymarketSnapshotCard";

const clients: QueryClient[] = [];
const slug = "will-example-happen";
function fixture(): PolymarketSnapshot {
  const now = Date.now() / 1000;
  return {
    exchange: "polymarket", environment: "production-public", product: "prediction", slug,
    question: "Will the example happen?", condition_id: "condition-example", version: "v2", read_at: now,
    active: true, ready: true, reasons: [], live_execution_enabled: false,
    outcomes: [
      { name: "Yes", token_id: "yes-token", bid: 0.35, ask: 0.37, midpoint: 0.36, timestamp: now, ready: true, reasons: [] },
      { name: "No", token_id: "no-token", bid: 0.63, ask: 0.65, midpoint: 0.64, timestamp: now, ready: true, reasons: [] },
    ],
  };
}
function response(snapshot = fixture()): Response {
  return new Response(JSON.stringify(snapshot), { headers: { "content-type": "application/json" } });
}
function createClient(): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 3, staleTime: Infinity } } });
  clients.push(client);
  return client;
}
function show(client = createClient()) {
  return { client, ...render(<QueryClientProvider client={client}><PolymarketSnapshotCard /></QueryClientProvider>) };
}
function enter(value = slug): void {
  fireEvent.change(screen.getByLabelText("Polymarket 市场 slug"), { target: { value } });
}
function submit(): void {
  fireEvent.click(screen.getByRole("button", { name: "读取公共概率快照" }));
}
async function fakeRead(): Promise<void> {
  enter();
  await act(async () => { submit(); await vi.advanceTimersByTimeAsync(1); });
}
function fakeTime(): void {
  vi.useFakeTimers({ toFake: ["Date", "performance", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date("2026-10-15T00:00:00Z"));
}

describe("independent Polymarket public snapshot", () => {
  afterEach(() => {
    cleanup(); clients.splice(0).forEach((client) => client.clear());
    vi.useRealTimers(); vi.unstubAllGlobals();
  });

  it("does not query until an explicitly submitted valid market slug", async () => {
    const fetch = vi.fn(async (_input: unknown) => response());
    vi.stubGlobal("fetch", fetch);
    show();
    expect(fetch).not.toHaveBeenCalled();
    enter();
    fireEvent.focus(window);
    expect(fetch).not.toHaveBeenCalled();
    submit();
    expect(await screen.findByText("公共概率报价可用")).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe(`/api/v3/prediction-markets/polymarket/${slug}`);
    expect(screen.getByText("production-public · 公共生产环境")).toBeInTheDocument();
    expect(screen.getByText("prediction · 预测市场")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.getByText("35.00%")).toBeInTheDocument();
    expect(screen.getByText("37.00%")).toBeInTheDocument();
    expect(screen.getByText("36.00%")).toBeInTheDocument();
    expect(screen.getByText(/概率数值仅为市场报价，不是预测或结果保证/)).toBeInTheDocument();
    expect(screen.queryByText(/网格|BTC|USDT/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /交易|下单|授权|live/i })).not.toBeInTheDocument();
  });

  it.each(["", "https://polymarket.com/event/example", "UPPER-case", "../market", "two--hyphens", "x".repeat(201)])("rejects invalid slug %s without a request", (input) => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    show(); enter(input); submit();
    expect(screen.getByRole("alert")).toHaveTextContent("请输入 1–200 字符");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("shows market restrictions as a warning while allowing ready public data", async () => {
    const snapshot = fixture(); snapshot.restricted = true; snapshot.reasons = ["market_restrictions_apply"];
    vi.stubGlobal("fetch", vi.fn(async () => response(snapshot)));
    show(); enter(); submit();
    expect(await screen.findByText("公共概率报价可用")).toBeInTheDocument();
    expect(screen.getByText("市场标记为受限")).toBeInTheDocument();
    expect(screen.getByText(/市场说明：market_restrictions_apply/)).toBeInTheDocument();
    expect(screen.getByText("35.00%")).toBeInTheDocument();
  });

  it("shows pending state and drops a prior market immediately when input changes", async () => {
    let resolveNext: ((value: Response) => void) | undefined;
    const fetch = vi.fn().mockResolvedValueOnce(response()).mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveNext = resolve; }));
    vi.stubGlobal("fetch", fetch);
    show(); enter(); submit();
    await screen.findByText("公共概率报价可用");
    submit();
    expect(await screen.findByRole("status")).toHaveTextContent("正在读取公共概率快照");
    expect(screen.queryByText("35.00%")).not.toBeInTheDocument();
    enter("another-market");
    expect(screen.queryByText("Will the example happen?")).not.toBeInTheDocument();
    expect(screen.queryByText("公共概率报价可用")).not.toBeInTheDocument();
    await act(async () => { resolveNext?.(response()); });
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Will the example happen?")).not.toBeInTheDocument();
    expect(screen.getByText("尚未确认市场，未发起公共行情查询。")).toBeInTheDocument();
  });

  it("hides every retained probability after a failed refresh without automatic retries", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response()).mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetch);
    show(); enter(); submit();
    await screen.findByText("公共概率报价可用");
    submit();
    expect(await screen.findByText("概率快照读取失败，当前报价已隐藏")).toBeInTheDocument();
    expect(screen.queryByText("公共概率报价可用")).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["empty outcomes", (data: PolymarketSnapshot) => { data.outcomes = []; }],
    ["one-sided bid", (data: PolymarketSnapshot) => { data.outcomes[0].bid = null; }],
    ["one-sided ask", (data: PolymarketSnapshot) => { data.outcomes[0].ask = null; }],
    ["crossed book", (data: PolymarketSnapshot) => { data.outcomes[0].bid = 0.9; }],
    ["out of bounds", (data: PolymarketSnapshot) => { data.outcomes[0].ask = 1.2; }],
    ["invalid midpoint", (data: PolymarketSnapshot) => { data.outcomes[0].midpoint = 0.8; }],
    ["missing timestamp", (data: PolymarketSnapshot) => { data.outcomes[0].timestamp = null; }],
    ["future timestamp", (data: PolymarketSnapshot) => { data.outcomes[0].timestamp = data.read_at + 10; }],
    ["stale book", (data: PolymarketSnapshot) => { data.outcomes[0].timestamp = data.read_at - 16; }],
    ["inactive market", (data: PolymarketSnapshot) => { data.active = false; }],
    ["unready outcome", (data: PolymarketSnapshot) => { data.outcomes[0].ready = false; }],
  ] as const)("does not mark %s ready even if top-level ready is true", async (_, mutate) => {
    const snapshot = fixture(); mutate(snapshot);
    vi.stubGlobal("fetch", vi.fn(async () => response(snapshot)));
    show(); enter(); submit();
    await screen.findByText("当前概率报价不可用");
    expect(screen.queryByText("公共概率报价可用")).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
  });

  it("rejects malformed snapshot identity instead of exposing another product's data", async () => {
    const snapshot = { ...fixture(), exchange: "binance", product: "spot" };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(snapshot))));
    show(); enter(); submit();
    await screen.findByText("概率快照读取失败，当前报价已隐藏");
    expect(screen.queryByText("Will the example happen?")).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
  });

  it("expires from the oldest book timestamp despite client wall-clock rollback", async () => {
    fakeTime();
    const snapshot = fixture(); snapshot.outcomes[0].timestamp = snapshot.read_at - 10;
    const fetch = vi.fn(async () => response(snapshot)); vi.stubGlobal("fetch", fetch);
    show(); await fakeRead();
    expect(screen.getByText("公共概率报价可用")).toBeInTheDocument();
    vi.setSystemTime(new Date("2026-10-14T00:00:00Z"));
    act(() => { vi.advanceTimersByTime(5_250); });
    expect(screen.getByText("公共概率快照已过期，请手动重新读取")).toBeInTheDocument();
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("removes current bid/ask at the 15-second evidence deadline without waiting for a poll", async () => {
    fakeTime();
    vi.stubGlobal("fetch", vi.fn(async () => response()));
    show(); await fakeRead();
    act(() => { vi.advanceTimersByTime(14_998); });
    expect(screen.getByText("公共概率报价可用")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.queryByText("公共概率报价可用")).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
  });

  it("charges full request duration against freshness before showing a delayed response", async () => {
    fakeTime(); const snapshot = fixture();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => {
      window.setTimeout(() => resolve(response(snapshot)), 16_000);
    })));
    const { client } = show(); enter();
    await act(async () => { submit(); await vi.advanceTimersByTimeAsync(16_001); });
    expect(client.getQueryData<ReceivedPolymarketSnapshot>(polymarketQueryKey(slug))?.requestDurationMs).toBeGreaterThanOrEqual(16_000);
    expect(screen.queryByText("公共概率报价可用")).not.toBeInTheDocument();
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
    expect(screen.getByText("公共概率快照已过期，请手动重新读取")).toBeInTheDocument();
  });

  it("does not auto-query or renew cached quotes on remount", async () => {
    fakeTime(); const snapshot = fixture();
    const fetch = vi.fn().mockResolvedValueOnce(response(snapshot)).mockImplementation(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", fetch);
    const { client } = show(); await fakeRead();
    const receipt = client.getQueryData<ReceivedPolymarketSnapshot>(polymarketQueryKey(slug))!.receivedMonotonicMs;
    expect(screen.getByText("公共概率报价可用")).toBeInTheDocument();
    cleanup(); act(() => { vi.advanceTimersByTime(16_000); }); show(client);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("公共概率报价可用")).not.toBeInTheDocument();
    enter(); submit();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(client.getQueryData<ReceivedPolymarketSnapshot>(polymarketQueryKey(slug))!.receivedMonotonicMs).toBe(receipt);
    expect(screen.queryByText(/\d+\.\d+%/)).not.toBeInTheDocument();
    expect(screen.getAllByText("不可用")).toHaveLength(6);
  });
});
