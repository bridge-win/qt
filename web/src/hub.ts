import { ApiError } from "./api";

const hubBase = (import.meta.env.VITE_QT_HUB_API_BASE ?? "/hub-api").replace(/\/$/, "");
const TOKEN_KEY = "qt-hub-token";

export function hubToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setHubToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export async function hubRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const token = hubToken();
  let response: Response;
  try {
    response = await fetch(`${hubBase}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    });
  } catch {
    throw new ApiError(path, null, "Unable to reach Agent Hub.");
  }
  if (response.status === 204) return undefined as T;
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(path, response.status, `Hub request failed (${response.status})`);
  }
  return body as T;
}

export function hubPost<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  return hubRequest<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
}
