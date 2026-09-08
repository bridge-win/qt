export type JsonObject = Record<string, unknown>;

export class ApiError extends Error {
  readonly endpoint: string;
  readonly status: number | null;
  readonly kind: "unauthorized" | "forbidden" | "missing" | "offline" | "failed";

  constructor(endpoint: string, status: number | null, message: string) {
    super(message);
    this.name = "ApiError";
    this.endpoint = endpoint;
    this.status = status;
    this.kind = status === 401
      ? "unauthorized"
      : status === 403
        ? "forbidden"
        : status === 404
          ? "missing"
          : status === null
            ? "offline"
            : "failed";
  }
}

const apiBase = (import.meta.env.VITE_QT_API_BASE ?? "").replace(/\/$/, "");

function detail(body: unknown): string | null {
  if (typeof body === "string") return body;
  if (body && typeof body === "object" && "detail" in body) {
    const value = body.detail;
    if (typeof value === "string") return value;
    if (value && typeof value === "object" && "message" in value && typeof value.message === "string") return value.message;
  }
  return null;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase}${path}`, {
      ...init,
      credentials: "include",
      headers: { Accept: "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(path, null, "Unable to reach the research service. Check Cloudflare Access and the protected origin.");
  }

  if (response.status === 204) return undefined as T;
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const message = detail(body) ?? `Request failed (${response.status})`;
    throw new ApiError(path, response.status, message);
  }
  return body as T;
}

export function post<T>(path: string, body?: JsonObject, headers?: HeadersInit): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
}

export function apiErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === "missing") return "当前服务尚未提供这项研究能力；页面不会用本地数据替代。";
    if (error.kind === "unauthorized") return "需要完成访问验证后才能读取或修改研究资源。";
    if (error.kind === "forbidden") return "当前身份没有执行此操作的权限。";
    if (error.kind === "offline") return "无法连接研究服务。请确认服务已启动后重新读取。";
    return "研究服务暂时没有返回可用数据。请重新读取；持续失败时检查服务状态。";
  }
  return "研究服务返回了无法识别的响应。请重新读取。";
}
