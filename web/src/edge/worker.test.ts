import { webcrypto } from "node:crypto";
import { readFile } from "node:fs/promises";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import worker from "../../edge/worker";

const encoder = new TextEncoder();
const base64Url = (input: Uint8Array | string): string => {
  const bytes = typeof input === "string" ? encoder.encode(input) : input;
  return Buffer.from(bytes).toString("base64url");
};

const env = {
  ASSETS: { fetch: vi.fn(async () => new Response("<html>private app</html>", { headers: { "content-type": "text/html" } })) },
  REPORTS: { get: vi.fn(async () => null) },
  ACCESS_JWT_ISSUER: "https://team.example",
  ACCESS_JWT_AUDIENCE: "audience-1",
  ORIGIN_URL: "https://origin.example",
  ORIGIN_CLIENT_ID: "worker-id",
  ORIGIN_CLIENT_SECRET: "worker-secret",
  MAX_API_BODY_BYTES: "32",
};

let privateKey: CryptoKey;
let publicJwk: JsonWebKey;

beforeAll(async () => {
  Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
  const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  privateKey = pair.privateKey;
  publicJwk = await crypto.subtle.exportKey("jwk", pair.publicKey);
});

async function accessToken(overrides: Record<string, unknown> = {}, kid = "edge-test"): Promise<string> {
  const header = base64Url(JSON.stringify({ alg: "RS256", kid }));
  const claims = base64Url(JSON.stringify({ iss: env.ACCESS_JWT_ISSUER, aud: env.ACCESS_JWT_AUDIENCE, exp: Math.floor(Date.now() / 1000) + 60, sub: "user-1", ...overrides }));
  const signature = new Uint8Array(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, encoder.encode(`${header}.${claims}`)));
  return `${header}.${claims}.${base64Url(signature)}`;
}

describe("QT edge worker", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("rejects a forged identity email without a signed Access JWT before serving assets", async () => {
    const response = await worker.fetch(new Request("https://preview.workers.dev/", { headers: { "cf-access-authenticated-user-email": "admin@example.com" } }), env);
    expect(response.status).toBe(401);
    expect(await response.text()).not.toContain("private app");
  });

  it("accepts a valid signature and rejects expired, future, wrong-claim, unknown-key, and tampered JWTs", async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const address = String(input);
      if (address.startsWith(env.ACCESS_JWT_ISSUER)) return new Response(JSON.stringify({ keys: [{ ...publicJwk, kid: "edge-test", alg: "RS256" }] }), { headers: { "content-type": "application/json" } });
      return new Response("unexpected origin", { status: 500 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const validResponse = await worker.fetch(new Request("https://preview.workers.dev/", { headers: { "cf-access-jwt-assertion": await accessToken() } }), env);
    expect(validResponse.status).toBe(200);
    const expired = await accessToken({ exp: Math.floor(Date.now() / 1000) - 1 });
    const future = await accessToken({ nbf: Math.floor(Date.now() / 1000) + 60 });
    const wrongIssuer = await accessToken({ iss: "https://attacker.example" });
    const wrongAudience = await accessToken({ aud: "attacker-audience" });
    const unknownKey = await accessToken({}, "rotated-key-not-present");
    const valid = await accessToken();
    const tampered = `${valid.slice(0, -1)}${valid.endsWith("a") ? "b" : "a"}`;
    for (const token of [expired, future, wrongIssuer, wrongAudience, unknownKey, tampered]) {
      const response = await worker.fetch(new Request("https://preview.workers.dev/", { headers: { "cf-access-jwt-assertion": token } }), env);
      expect(response.status).toBe(401);
    }
    expect(fetchMock.mock.calls.filter(([input]) => String(input).startsWith(env.ACCESS_JWT_ISSUER)).length).toBeGreaterThanOrEqual(2);
  });

  it("accepts the same Access issuer with trailing slash differences", async () => {
    const slashEnv = { ...env, ACCESS_JWT_ISSUER: `${env.ACCESS_JWT_ISSUER}/` };
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      const address = String(input);
      if (address.startsWith(env.ACCESS_JWT_ISSUER)) return new Response(JSON.stringify({ keys: [{ ...publicJwk, kid: "edge-test", alg: "RS256" }] }), { headers: { "content-type": "application/json" } });
      return new Response("unexpected origin", { status: 500 });
    }));
    const response = await worker.fetch(new Request("https://preview.workers.dev/", { headers: { "cf-access-jwt-assertion": await accessToken() } }), slashEnv);
    expect(response.status).toBe(200);
  });

  it("preserves API 404 JSON instead of returning SPA HTML", async () => {
    const token = await accessToken();
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      const address = String(input);
      if (address.startsWith(env.ACCESS_JWT_ISSUER)) return new Response(JSON.stringify({ keys: [{ ...publicJwk, kid: "edge-test", alg: "RS256" }] }), { headers: { "content-type": "application/json" } });
      return new Response(JSON.stringify({ detail: { code: "unknown_result" } }), { status: 404, headers: { "content-type": "application/json" } });
    }));
    const response = await worker.fetch(new Request("https://preview.workers.dev/api/v3/results/missing", { headers: { "cf-access-jwt-assertion": token } }), env);
    expect(response.status).toBe(404);
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(await response.text()).toContain("unknown_result");
  });

  it("caps chunked API bodies and denies traversal while forcing R2 HTML to download", async () => {
    const token = await accessToken();
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => new Response(JSON.stringify({ keys: [{ ...publicJwk, kid: "edge-test", alg: "RS256" }] }), { headers: { "content-type": "application/json" } })));
    const oversized = new Request("https://preview.workers.dev/api/v3/notes", { method: "POST", headers: { "cf-access-jwt-assertion": token }, body: "x".repeat(33) });
    expect((await worker.fetch(oversized, env)).status).toBe(413);
    const traversal = await worker.fetch(new Request("https://preview.workers.dev/artifacts/%252e%252e/secret", { headers: { "cf-access-jwt-assertion": token } }), env);
    expect(traversal.status).toBe(404);
    expect(env.REPORTS.get).not.toHaveBeenCalled();
    const reportEnv = { ...env, REPORTS: { get: vi.fn(async () => ({ body: new Response("<script>bad()</script>").body, httpMetadata: { contentType: "text/html", contentDisposition: "inline" }, writeHttpMetadata: (headers: Headers) => { headers.set("content-type", "text/html"); headers.set("content-disposition", "inline"); } })) } };
    const report = await worker.fetch(new Request("https://preview.workers.dev/artifacts/runs/a.html", { headers: { "cf-access-jwt-assertion": token } }), reportEnv);
    expect(report.status).toBe(200);
    expect(report.headers.get("content-disposition")).toContain("attachment");
    expect(report.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("uses worker-first routing for every static request in the real merge fragment", async () => {
    const config = await readFile(`${process.cwd()}/edge/wrangler.fragment.toml`, "utf8");
    expect(config).toMatch(/run_worker_first\s*=\s*true/);
  });
});
