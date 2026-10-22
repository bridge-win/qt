import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HubInboxPage } from "./HubInboxPage";
import { HubRunPage } from "./HubRunPage";
import { HubSchedulesPage } from "./HubSchedulesPage";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

function wrapper(ui: ReactElement, path = "/hub"): ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("Agent Hub workbench pages", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("shows empty inbox with the next-run message and can approve once", async () => {
    localStorage.setItem("qt-hub-token", "test-token");
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith("/inbox")) {
        return json({
          approvals: [{ id: "a1", run_id: "run-1", risk_tier: "funds", expires_at: Date.now() / 1000 + 60 }],
          failed_jobs: [],
          next_run_at: 1,
          next_run_in_seconds: 42,
          timezone: "UTC",
          empty_reason: null,
        });
      }
      if (path.includes("/approvals/a1") && init?.method === "POST") return json({ id: "a1", decision: "once" });
      return json({ items: [] });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(wrapper(<HubInboxPage />));
    expect(await screen.findByText("收件箱")).toBeInTheDocument();
    await userEvent.click(await screen.findByText("once"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/approvals/a1"), expect.objectContaining({ method: "POST" })));
  });

  it("renders the three-column run view with an evidence panel", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      if (path.includes("/diagnostics")) return json({ cpcv: { paths: 3 }, dsr: { n_trials: 1 } });
      return json({
        run_id: "run-1",
        items: [
          { id: "j1", kind: "forecast", status: "succeeded", result: { proposal: { evidence_hash: "abc", time_boundary: { expires_at: 1 } }, evidence_validation: { valid: true } } },
        ],
        truncated: false,
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/hub/runs/run-1"]}>
          <Routes>
            <Route path="/hub/runs/:runId" element={<HubRunPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("run-1")).toBeInTheDocument();
    expect(screen.getByText("证据")).toBeInTheDocument();
    expect(screen.getByText("ThoughtChain")).toBeInTheDocument();
  });

  it("lists schedules with absolute, relative and timezone fields", async () => {
    localStorage.setItem("qt-hub-token", "test-token");
    vi.stubGlobal("fetch", vi.fn(async () => json({
      items: [{ id: "s1", name: "monitor", kind: "monitor", overlap_policy: "skip", catchup_window_s: 900, next_run_at: Date.now() / 1000 + 30, notify_only_on_change: true, enabled: true }],
    })));
    render(wrapper(<HubSchedulesPage />, "/hub/schedules"));
    expect(await screen.findAllByText("monitor")).not.toHaveLength(0);
    expect(screen.getByText("skip")).toBeInTheDocument();
    expect(screen.getByText("下次运行(UTC)")).toBeInTheDocument();
  });
});
