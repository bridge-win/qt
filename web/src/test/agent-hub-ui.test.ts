import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fireEvent, waitFor, within } from "@testing-library/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Execute the actual served HTML script against a DOM. API mocks deliberately
// avoid Codex/network/real ledger effects; API integration has separate tests.
const html = readFileSync(resolve(process.cwd(), "../src/qt/agent_hub/static/index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)![1];

type Job = { id: string; kind: string; status: string; attempts: number; created_at: number; error?: string; payload: Record<string, unknown>; result?: Record<string, unknown> };
let jobs: Job[];
let riskMode: string;
let readinessFailure: boolean;
let fetchMock: ReturnType<typeof vi.fn>;

function element<T extends HTMLElement = HTMLElement>(id: string): T {
  const value = document.getElementById(id);
  if (!value) throw new Error(`Missing served element: ${id}`);
  return value as T;
}

function fill(id: string, value: string): void {
  fireEvent.input(element(id), { target: { value } });
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

async function login(): Promise<void> {
  fill("token", "isolated-dom-test-token");
  fireEvent.submit(element("loginForm"));
  await waitFor(() => expect(element("workspace").hidden).toBe(false));
}

function calls(path: string): Array<{ method: string; body: Record<string, unknown> }> {
  return fetchMock.mock.calls.filter(([url]) => url === path).map(([, options]) => ({
    method: options.method,
    body: options.body ? JSON.parse(options.body) : {},
  }));
}

describe("served Agent Hub v2 UI interaction contracts", () => {
  beforeEach(() => {
    jobs = []; riskMode = "active"; readinessFailure = false;
    document.body.innerHTML = html.replace(/<script>[\s\S]*?<\/script>/, "").match(/<body>([\s\S]*?)<\/body>/)![1];
    fetchMock = vi.fn(async (path: string, options: RequestInit) => {
      expect(options.headers).toEqual({ Authorization: "Bearer isolated-dom-test-token", "Content-Type": "application/json" });
      const body = options.body ? JSON.parse(String(options.body)) : {};
      if (path === "/api/status") return json({ mode: "paper", tenant_id: "alpha", queue: { counts: { queued: jobs.filter(j => j.status === "queued").length } }, workers: [], trading: { risk_mode: riskMode } });
      if (path === "/api/jobs" && options.method === "GET") return json({ items: jobs });
      if (path === "/api/jobs" && options.method === "POST") {
        const job: Job = { id: "queued-job", kind: body.kind, status: "queued", attempts: 0, created_at: 1, payload: body };
        jobs.push(job); return json(job, 202);
      }
      if (path.startsWith("/api/jobs/") && path.endsWith("/cancel")) {
        jobs = jobs.map(job => ({ ...job, status: "deadletter", error: "cancelled_by_owner" }));
        return json({ status: "cancelled", cancelled_jobs: jobs.length });
      }
      if (path.startsWith("/api/jobs/")) return json(jobs.find(job => path.endsWith(job.id)));
      if (path === "/api/risk-mode") { riskMode = body.mode; return json({ risk_mode: riskMode }); }
      if (path.startsWith("/api/traces?")) return json({ items: [{ trace_id: "fixture-trace", run_id: "root-run", metadata: { evidence_valid: true } }] });
      if (["/api/schedules", "/api/events", "/api/skills", "/api/memories"].includes(path)) return json({ items: [] });
      if (path === "/api/capabilities") return json({ version: "2.0.0", mode: "paper" });
      if (path === "/api/release/readiness") return readinessFailure ? json({ detail: "temporary fixture failure" }, 503) : json({ paper_ready: false, live_ready: false, checks: [{ id: "live_acceptance", status: "unverified", detail: "No funded account test" }] });
      if (path === "/api/documents") return json({ id: "document", revision: 2 });
      if (path.startsWith("/api/search?")) return json({ items: [{ id: "document", content: "<img src=x onerror=alert(1)>" }] });
      throw new Error(`Unexpected UI request ${path}`);
    });
    new Function("window", "document", "fetch", "setInterval", "setTimeout", "crypto", "location", script)(
      window, document, fetchMock, () => 0, () => 0, { randomUUID: () => "ui-request-fixture" }, window.location,
    );
  });

  afterEach(async () => {
    // Event handlers await response.json() and follow-up refreshes. Let those
    // complete before replacing the DOM for the next independent test.
    await new Promise(resolve => setTimeout(resolve, 0));
    document.body.replaceChildren(); vi.restoreAllMocks();
  });

  it("does not fetch before login and keeps the token out of storage and rendered data", async () => {
    expect(fetchMock).not.toHaveBeenCalled();
    await login();
    expect(element<HTMLInputElement>("token").value).toBe("");
    expect(element("login").hidden).toBe(true);
    expect(document.body.textContent).not.toContain("isolated-dom-test-token");
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
    await waitFor(() => expect(element("releaseChecks").textContent).toContain("unverified"));
  });

  it("shows the workspace even when release readiness is temporarily unavailable", async () => {
    readinessFailure = true;
    await login();
    await waitFor(() => expect(element("notice").textContent).toContain("503"));
    expect(element("workspace").hidden).toBe(false);
  });

  it("submits a version-bound strategy and retains research-only by default", async () => {
    await login(); fill("question", "Test BTC evidence");
    fireEvent.submit(element("jobForm"));
    await waitFor(() => expect(calls("/api/jobs").some(call => call.method === "POST")).toBe(true));
    const sent = calls("/api/jobs").find(call => call.method === "POST")!.body;
    expect(sent).toMatchObject({ strategy: "ma_trend", auto_execute: false, venue: "binance", instrument: "BTCUSDT", idempotency_key: "ui-request-fixture" });
    expect(sent).not.toHaveProperty("tenant_id");
  });

  it("changes the three explicit risk modes and renders the returned state", async () => {
    await login();
    for (const [id, mode] of [["reduce", "reduce_only"], ["kill", "frozen"], ["resume", "active"]]) {
      fireEvent.click(element(id));
      await waitFor(() => expect(element("riskMode").textContent).toContain(mode));
    }
    expect(calls("/api/risk-mode").map(call => call.body.mode)).toEqual(["reduce_only", "frozen", "active"]);
  });

  it("cancels a workflow and removes its misleading retry control", async () => {
    jobs = [{ id: "run", kind: "research", status: "queued", attempts: 0, created_at: 1, payload: {} }];
    await login();
    fireEvent.click(within(element("jobs")).getByRole("button", { name: "取消流程" }));
    await waitFor(() => expect(element("jobs").textContent).toContain("cancelled"));
    expect(within(element("jobs")).queryByRole("button", { name: "重试" })).toBeNull();
    expect(calls("/api/jobs/run/cancel")).toHaveLength(1);
  });

  it("shows bound evidence and queries the parent run trace from task details", async () => {
    jobs = [{ id: "child", kind: "forecast", status: "succeeded", attempts: 1, created_at: 1, payload: { pipeline_id: "root-run" }, result: { evidence_validation: { valid: true }, proposal: { proposal_hash: "bound-hash" } } }];
    await login();
    fireEvent.click(within(element("jobs")).getByRole("button", { name: "详情" }));
    await waitFor(() => expect(element("runEvidence").textContent).toContain("bound-hash"));
    expect(element("jobDetailPanel").hidden).toBe(false);
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => path.includes("run_id=root-run"))).toBe(true));
  });

  it("passes explicit historical availability with provenance and renders source content as text", async () => {
    await login();
    fill("source", "archive:official"); fill("content", "Original historical evidence");
    fill("validFrom", "2026-09-01T12:00"); fill("availableAt", "2026-09-02T12:00"); fill("provenance", "release:official-archive");
    fireEvent.submit(element("documentForm"));
    await waitFor(() => expect(calls("/api/documents")).toHaveLength(1));
    expect(calls("/api/documents")[0].body).toMatchObject({ provenance: "release:official-archive", available_at: new Date("2026-09-02T12:00").getTime() / 1000 });
    fill("asOf", "2026-09-20T12:00"); fill("knownAt", "2026-10-20T12:00");
    fireEvent.submit(element("searchForm"));
    await waitFor(() => expect(element("searchResult").textContent).toContain("<img"));
    expect(element("searchResult").querySelector("img")).toBeNull();
    expect(fetchMock.mock.calls.some(([path]) => path.startsWith("/api/search?") && path.includes("as_of=") && path.includes("known_at="))).toBe(true);
  });
});
