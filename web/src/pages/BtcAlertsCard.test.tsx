import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BtcAlertsCard, type AlertsProjection } from "./BtcAlertsCard";

const clients: QueryClient[] = [];
function fixture(): AlertsProjection {
  return {
    configured: true, server_time: Date.now() / 1000, revision: 1, strategy_version: "strategy-fixture",
    policy: { mode: "shadow", thresholds: { breakout_volume: 1.5, distress_rsi: 25, distress_volume: 2, wick_body: 2, wick_fraction: 0.6, wick_atr: 1.5, overheat_rsi: 75, overheat_recovery_rsi: 70 } },
    worker: { alive: true, status: "watching", notifications_configured: false, recipient_verified: false,
      markets: { spot: { ready: true }, perpetual: { ready: false, reason: "funding_context_insufficient" } },
      reviewer: { authenticated: false } },
    readiness: { software: true, model: false, recipient: false, strategies: false },
    cards: [{ strategy_id: "trend", name: "趋势突破", counterexample: "突破也可能失败", rule_origin: "工程假设",
      sources: [{ evidence_id: "paper", title: "原始论文", url: "https://example.test/paper" }] }],
    validations: [{ strategy_id: "trend", direction: "long", status: "unvalidated", oos_events: 0 }],
    events: [{ event_id: "candidate-fixture-1", created_at: Date.now() / 1000, status: "evidence_pending",
      reason: "validation_pending:trend:long", payload: { direction: "long" } }],
  };
}
function setup(value = fixture()) {
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) => {
    const response = init?.method === "PUT"
      ? { ...value, revision: 2, policy: JSON.parse(String(init.body)).policy }
      : value;
    return new Response(JSON.stringify(response), { headers: { "content-type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetcher);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><BtcAlertsCard /></QueryClientProvider>);
  return fetcher;
}

describe("BTC reviewed opportunity alerts", { timeout: 15_000 }, () => {
  afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("shows validation blockers, citations and a shared hourly limit without order controls", async () => {
    setup();
    expect(await screen.findByText("strategy-fixture")).toBeInTheDocument();
    expect(screen.getByText(/任意连续 60 分钟最多一条/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "启用正式提醒" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送 QT 测试" })).toBeDisabled();
    expect(screen.getByText("验证证据不足")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "原始论文" })).toHaveAttribute("href", "https://example.test/paper");
    expect(screen.queryByRole("button", { name: /下单|实盘交易/ })).not.toBeInTheDocument();
  });

  it("creates a new shadow strategy version when saving a threshold", async () => {
    const fetcher = setup();
    const input = await screen.findByRole("spinbutton", { name: "突破成交量倍数" });
    fireEvent.change(input, { target: { value: "1.8" } });
    fireEvent.blur(input);
    fireEvent.click(screen.getByRole("button", { name: "保存为新策略版本" }));
    await waitFor(() => expect(fetcher.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    const [, init] = fetcher.mock.calls.find(([, value]) => value?.method === "PUT")!;
    const payload = JSON.parse(String(init!.body));
    expect(payload.expected_revision).toBe(1);
    expect(payload.policy.mode).toBe("shadow");
    expect(payload.policy.thresholds.breakout_volume).toBe(1.8);
  });

  it("queues a fixed test request only for a verified recipient", async () => {
    const value = fixture();
    value.worker.recipient_verified = true;
    const fetcher = setup(value);
    const button = await screen.findByRole("button", { name: "发送 QT 测试" });
    fireEvent.click(button);
    await waitFor(() => expect(fetcher.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
    const [url, init] = fetcher.mock.calls.find(([, value]) => value?.method === "POST")!;
    expect(url).toBe("/api/v3/btc-alerts/test-notification");
    expect(Object.keys(JSON.parse(String(init!.body)))).toEqual(["request_id"]);
  });

  it("shows unavailable host explicitly", async () => {
    setup({ configured: false } as AlertsProjection);
    expect(await screen.findByText("此主机尚未启动机会提醒服务")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "启用正式提醒" })).not.toBeInTheDocument();
  });

  it("does not offer actions from a dead worker", async () => {
    const value = fixture();
    value.worker.alive = false;
    value.worker.recipient_verified = true;
    value.readiness = { software: true, model: true, recipient: true, strategies: true };
    setup(value);
    expect(await screen.findByText("提醒服务状态不可确认")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "启用正式提醒" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送 QT 测试" })).toBeDisabled();
  });

  it("expires actionable status after 20 seconds even when refresh hangs and wall clock rolls back", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
    const value = fixture();
    value.worker.recipient_verified = true;
    const fetcher = setup(value);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByRole("button", { name: "发送 QT 测试" })).toBeEnabled();
    fetcher.mockImplementation(() => new Promise<Response>(() => {}));
    vi.setSystemTime(Date.now() - 86400_000);
    await act(async () => { await vi.advanceTimersByTimeAsync(20_001); });
    expect(screen.getByText("提醒服务状态不可确认")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送 QT 测试" })).toBeDisabled();
  });
});
