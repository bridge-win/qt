/**
 * Canonical QT edge entry point. The root Cloudflare deployment may import this
 * file, but it must not ship a second authentication implementation.
 */
interface AssetFetcher { fetch(input: Request | string): Promise<Response>; }
interface R2Object {
  body: ReadableStream<Uint8Array> | null;
  httpMetadata?: { contentType?: string; contentDisposition?: string };
  writeHttpMetadata(headers: Headers): void;
}
interface R2Bucket { get(key: string): Promise<R2Object | null>; }
interface Env {
  ASSETS: AssetFetcher;
  REPORTS: R2Bucket;
  ACCESS_JWT_ISSUER: string;
  ACCESS_JWT_AUDIENCE: string;
  ORIGIN_URL: string;
  ORIGIN_CLIENT_ID: string;
  ORIGIN_CLIENT_SECRET: string;
  MAX_API_BODY_BYTES?: string;
}
interface AccessClaims { iss: string; aud: string | string[]; exp: number; nbf?: number; sub: string; email?: string; }
interface Jwk { kid?: string; kty: string; n?: string; e?: string; alg?: string; use?: string; }

const accessHeader = "cf-access-jwt-assertion";
const maxArtifactKeyLength = 480;
const jwks = new Map<string, { expiresAt: number; keys: Jwk[] }>();

function textResponse(status: number, message: string): Response {
  return new Response(JSON.stringify({ detail: { code: `edge_${status}`, message } }), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" } });
}

function base64UrlBytes(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("invalid base64url");
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(padded);
  if (btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") !== value) throw new Error("non-canonical base64url");
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function parsePart<T>(value: string): T {
  return JSON.parse(new TextDecoder().decode(base64UrlBytes(value))) as T;
}

function arrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

function normalizedIssuer(value: string): string {
  const issuer = value.trim().replace(/\/+$/, "");
  if (!issuer.startsWith("https://")) throw new Error("Access JWT issuer must use HTTPS");
  return issuer;
}

function trustedAccessIssuer(configuredIssuer: string, tokenIssuer: string): string {
  const configured = normalizedIssuer(configuredIssuer);
  const token = normalizedIssuer(tokenIssuer);
  if (token === configured) return configured;
  const hostname = new URL(token).hostname;
  if (hostname === "cloudflareaccess.com" || hostname.endsWith(".cloudflareaccess.com")) return token;
  throw new Error("Access JWT issuer does not match this deployment");
}

async function certificateKeys(issuer: string, refresh = false): Promise<Jwk[]> {
  const normalized = normalizedIssuer(issuer);
  const cached = jwks.get(normalized);
  if (!refresh && cached && cached.expiresAt > Date.now()) return cached.keys;
  const response = await fetch(`${normalized}/cdn-cgi/access/certs`, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error("Access certificate endpoint unavailable");
  const payload = await response.json() as { keys?: unknown };
  if (!Array.isArray(payload.keys)) throw new Error("Access certificate response is invalid");
  const keys = payload.keys.filter((key): key is Jwk => Boolean(key) && typeof key === "object" && (key as Jwk).kty === "RSA" && (key as Jwk).alg === "RS256");
  if (keys.length === 0) throw new Error("No compatible Access signing key");
  jwks.set(normalized, { keys, expiresAt: Date.now() + 5 * 60_000 });
  return keys;
}

async function verifyAccessJwt(token: string, env: Env): Promise<AccessClaims> {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Malformed Access JWT");
  const header = parsePart<{ alg?: string; kid?: string }>(parts[0]);
  const claims = parsePart<AccessClaims>(parts[1]);
  if (header.alg !== "RS256" || !header.kid) throw new Error("Unsupported Access JWT signing algorithm");
  const issuer = trustedAccessIssuer(env.ACCESS_JWT_ISSUER, claims.iss);
  if (!Number.isFinite(claims.exp) || claims.exp <= Math.floor(Date.now() / 1000)) throw new Error("Access JWT has expired");
  if (claims.nbf !== undefined && (!Number.isFinite(claims.nbf) || claims.nbf > Math.floor(Date.now() / 1000))) throw new Error("Access JWT is not active yet");
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!audiences.includes(env.ACCESS_JWT_AUDIENCE)) throw new Error("Access JWT audience does not match this deployment");
  if (!claims.sub || typeof claims.sub !== "string") throw new Error("Access JWT subject is missing");
  let key = (await certificateKeys(issuer)).find((candidate) => candidate.kid === header.kid);
  if (!key) key = (await certificateKeys(issuer, true)).find((candidate) => candidate.kid === header.kid);
  if (!key) throw new Error("Access JWT signing key is unknown");
  const publicKey = await crypto.subtle.importKey("jwk", key, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", publicKey, arrayBuffer(base64UrlBytes(parts[2])), arrayBuffer(new TextEncoder().encode(`${parts[0]}.${parts[1]}`)));
  if (!valid) throw new Error("Access JWT signature is invalid");
  return claims;
}

async function requireAccess(request: Request, env: Env): Promise<{ claims: AccessClaims; token: string } | Response> {
  const token = request.headers.get(accessHeader);
  if (!token) return textResponse(401, "A signed Cloudflare Access JWT is required.");
  try { return { claims: await verifyAccessJwt(token, env), token }; }
  catch (error) { return textResponse(401, error instanceof Error ? error.message : "Access JWT validation failed."); }
}

function safeArtifactKey(pathname: string): string | null {
  const raw = pathname.slice("/artifacts/".length);
  if (!raw || raw.length > maxArtifactKeyLength) return null;
  let decoded: string;
  try { decoded = decodeURIComponent(raw); } catch { return null; }
  if (!/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(decoded)) return null;
  if (decoded.split("/").some((part) => part === "." || part === ".." || part.length === 0)) return null;
  return `reports/${decoded}`;
}

function apiBodyLimit(env: Env): number {
  const configured = Number(env.MAX_API_BODY_BYTES ?? 1_048_576);
  if (!Number.isFinite(configured) || configured <= 0) throw new Error("The edge API body limit is not configured safely.");
  return Math.min(Math.floor(configured), 10 * 1_048_576);
}

async function boundedBody(request: Request, limit: number): Promise<ArrayBuffer | undefined> {
  if (request.method === "GET" || request.method === "HEAD") return undefined;
  const advertised = request.headers.get("content-length");
  if (advertised !== null && (!/^\d+$/.test(advertised) || Number(advertised) > limit)) throw new Error("Request body exceeds the edge limit.");
  if (!request.body) return new ArrayBuffer(0);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      total += next.value.byteLength;
      if (total > limit) { await reader.cancel("edge request body limit exceeded"); throw new Error("Request body exceeds the edge limit."); }
      chunks.push(next.value);
    }
  } finally { reader.releaseLock(); }
  const output = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.byteLength; }
  return output.buffer;
}

async function proxyApi(request: Request, env: Env, claims: AccessClaims, token: string): Promise<Response> {
  if (!["GET", "HEAD", "POST", "PATCH", "DELETE"].includes(request.method)) return textResponse(405, "This API method is not allowed by the edge proxy.");
  let destination: URL;
  try {
    destination = new URL(request.url);
    const origin = new URL(env.ORIGIN_URL);
    if (origin.protocol !== "https:") throw new Error("Origin must use HTTPS");
    destination.protocol = origin.protocol; destination.hostname = origin.hostname; destination.port = origin.port;
  } catch { return textResponse(500, "The protected origin is not configured safely."); }
  let body: ArrayBuffer | undefined;
  try { body = await boundedBody(request, apiBodyLimit(env)); }
  catch (error) { return textResponse(413, error instanceof Error ? error.message : "Request body rejected."); }
  const headers = new Headers(request.headers);
  for (const header of ["authorization", "cookie", "host", "cf-access-authenticated-user-email", "cf-access-authenticated-user-id", "x-qt-user", "x-qt-access-subject", "cf-access-client-id", "cf-access-client-secret"]) headers.delete(header);
  headers.set(accessHeader, token);
  headers.set("cf-access-client-id", env.ORIGIN_CLIENT_ID);
  headers.set("cf-access-client-secret", env.ORIGIN_CLIENT_SECRET);
  headers.set("x-qt-access-subject", claims.sub);
  headers.set("x-qt-access-email", claims.email ?? "");
  try {
    const response = await fetch(destination, { method: request.method, headers, body, redirect: "manual" });
    const responseHeaders = new Headers(response.headers);
    responseHeaders.set("cache-control", "no-store");
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers: responseHeaders });
  } catch { return textResponse(503, "The protected research origin is offline or unreachable."); }
}

async function privateArtifact(request: Request, env: Env, pathname: string): Promise<Response> {
  if (request.method !== "GET" && request.method !== "HEAD") return textResponse(405, "Artifacts are read-only.");
  const key = safeArtifactKey(pathname);
  if (!key) return textResponse(404, "Artifact does not exist.");
  const object = await env.REPORTS.get(key);
  if (!object) return textResponse(404, "Artifact does not exist.");
  const headers = new Headers({ "cache-control": "private, no-store" });
  object.writeHttpMetadata(headers);
  const filename = key.split("/").at(-1)?.replace(/[^A-Za-z0-9._-]/g, "_") ?? "artifact";
  headers.set("content-disposition", `attachment; filename="${filename}"`);
  headers.set("x-content-type-options", "nosniff");
  headers.set("cache-control", "private, no-store");
  return new Response(request.method === "HEAD" ? null : object.body, { headers });
}

function secureAssetResponse(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("x-content-type-options", "nosniff"); headers.set("x-frame-options", "DENY"); headers.set("referrer-policy", "same-origin");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

const worker = {
  async fetch(request: Request, env: Env): Promise<Response> {
    const access = await requireAccess(request, env);
    if (access instanceof Response) return access;
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return proxyApi(request, env, access.claims, access.token);
    if (url.pathname.startsWith("/artifacts/")) return privateArtifact(request, env, url.pathname);
    if (request.method !== "GET" && request.method !== "HEAD") return textResponse(405, "Static assets are read-only.");
    return secureAssetResponse(await env.ASSETS.fetch(request));
  },
};

export default worker;
