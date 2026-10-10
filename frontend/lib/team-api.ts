import { appPath } from "./app-path";

export class ApiError extends Error {
  constructor(public code: string, message: string, public status = 0) { super(message); this.name = "ApiError"; }
}

const businessRequests = new Set<AbortController>();
let boundContext: string | null | undefined;
let boundUser: TeamUser | null = null;
export function bindDesignContext(context: string | null, user: TeamUser | null = null) {
  if ((boundUser?.access_mode === "demo" || user?.access_mode === "demo") && (context !== boundContext || (user && boundUser && userIdentity(user) !== userIdentity(boundUser)))) {
    for (const request of businessRequests) request.abort();
    businessRequests.clear();
  }
  boundContext = context; boundUser = user;
}
export function designContextKey() { return boundContext ?? "legacy"; }
export function currentDesignUser() { return boundUser; }
export const DESIGN_SESSION_INVALIDATED_EVENT = "design-session-invalidated";

const fallback: Record<number, string> = {
  401: "登录已失效，请重新登录。",
  403: "当前账号没有执行此操作的权限。",
  404: "找不到这条记录，或你已不再负责这项任务。",
  409: "记录已更新。你的编辑仍保留，请重新核对当前版本。",
  410: "此入口已停用，请使用当前设计任务入口。",
  422: "填写内容不符合要求，请检查后重试。",
  429: "操作过于频繁，请稍后重试。",
  503: "这项服务暂不可用，已保存的内容仍然保留。",
};

/** Same-origin, no automatic write retries, caller cancellation plus bounded timeout. */
export async function teamRequest<T>(path: string, init: RequestInit = {}, timeoutMs = 30000): Promise<T> {
  const controller = new AbortController();
  const endpoint = path.split("?")[0];
  const authRequest = ["/design-auth/me", "/design-auth/login", "/design-auth/demo-access"].includes(endpoint);
  if (!authRequest) businessRequests.add(controller);
  const relay = () => controller.abort();
  if (init.signal?.aborted) controller.abort();
  else init.signal?.addEventListener("abort", relay, { once: true });
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  try {
    const headers = new Headers(init.headers);
    const writing = !["GET", "HEAD"].includes((init.method ?? "GET").toUpperCase());
    if (writing && !["/design-auth/login", "/design-auth/demo-access"].includes(endpoint) && (boundContext !== undefined || headers.has("X-Design-Context"))) {
      if (!headers.has("X-Design-Context")) {
        if (!boundContext) throw new ApiError("CONTEXT_REQUIRED", "正在核对账号，请等待身份确认后再操作。");
        headers.set("X-Design-Context", boundContext);
      }
      if (!headers.has("X-Design-Action-Id")) headers.set("X-Design-Action-Id", crypto.randomUUID());
    }
    const response = await fetch(appPath(`/api${path}`), { ...init, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    const body: unknown = await response.json().catch(() => null);
    if (controller.signal.aborted) throw new DOMException("Request cancelled", "AbortError");
    businessRequests.delete(controller);
    if (!response.ok) {
      const object = body && typeof body === "object" ? body as Record<string, unknown> : {};
      const error = object.error && typeof object.error === "object" ? object.error as Record<string, unknown> : {};
      // Never stringify arbitrary validation payloads or provider traces into a product message.
      const code = typeof error.code === "string" ? error.code : `HTTP_${response.status}`;
      const message = typeof error.message === "string" ? error.message : typeof object.detail === "string" ? object.detail : fallback[response.status] ?? "操作未完成，请稍后重试。";
      const endpoint = path.split("?")[0];
      if (!["/design-auth/login", "/design-auth/me"].includes(endpoint) && (response.status === 401 || (response.status === 409 && code === "AUTH_CONTEXT_CHANGED"))) {
        bindDesignContext(null);
        // Invalidate this tab only, without credentials, identity data, or pending-action storage.
        if (typeof window !== "undefined") window.dispatchEvent(new Event(DESIGN_SESSION_INVALIDATED_EVENT));
      }
      throw new ApiError(code, message, response.status);
    }
    if (response.status === 204) return undefined as T;
    if (!body || typeof body !== "object") throw new ApiError("INVALID_RESPONSE", "服务返回格式异常，请重新连接。", response.status);
    return body as T;
  } catch (error) {
    if (controller.signal.aborted) {
      if (init.signal?.aborted || !timedOut) throw new DOMException("Request cancelled", "AbortError");
      throw new ApiError("RESPONSE_UNKNOWN", "未能确认操作结果。请先刷新核对记录，再决定是否重试原操作。");
    }
    if (error instanceof ApiError) throw error;
    throw new ApiError("NETWORK_UNAVAILABLE", "连接中断，操作结果尚未确认。你的编辑仍保留，请先重新连接。");
  } finally {
    businessRequests.delete(controller);
    clearTimeout(timeout);
    init.signal?.removeEventListener("abort", relay);
  }
}

export function jsonBody(value: unknown, method = "POST"): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) };
}

export type TeamUser = { username: string; display_name: string; role: "manager" | "designer"; integration_mode?: "managed"; auth_context_id?: string; instance_id?: string; scope_id?: string; access_mode?: "demo" | "authenticated"; demo_entry_urls?: { designer: "/demo/designer"; manager: "/demo/manager" } | null };
export type TeamMember = { username: string; display_name: string; active: boolean };
export function userIdentity(user: TeamUser): string { return `${user.instance_id ?? ""}:${user.scope_id ?? ""}:${user.username}:${user.role}:${user.auth_context_id ?? ""}:${user.access_mode ?? "authenticated"}`; }
/** Demo drafts/actions never cross role or context. Existing authenticated drafts keep their legacy keys. */
export function storageOwner(user: TeamUser) {
  const parts = [user.instance_id, user.scope_id, user.username];
  return JSON.stringify(user.access_mode === "demo" ? [...parts, "demo", user.role, user.auth_context_id] : parts);
}
export function validateTeamUser(user: TeamUser): TeamUser {
  if (typeof user.username !== "string" || !user.username.trim() || typeof user.display_name !== "string" || !user.display_name.trim() || !["manager", "designer"].includes(user.role)) throw new ApiError("IDENTITY_UNKNOWN", "无法确认当前账号权限，请重新连接。");
  if (user.integration_mode === "managed" || user.access_mode === "demo") {
    if ([user.auth_context_id, user.instance_id, user.scope_id].some(value => typeof value !== "string" || !value.trim())) throw new ApiError("IDENTITY_UNKNOWN", "工作区身份信息不完整，请重新连接。");
  }
  if (user.access_mode !== undefined && !["demo", "authenticated"].includes(user.access_mode)) throw new ApiError("IDENTITY_UNKNOWN", "无法确认工作区访问方式。");
  if (user.access_mode === "demo" && (user.demo_entry_urls?.designer !== "/demo/designer" || user.demo_entry_urls?.manager !== "/demo/manager")) throw new ApiError("IDENTITY_UNKNOWN", "演示入口信息不完整，请重新连接。");
  if (user.access_mode === "authenticated" && user.demo_entry_urls !== null) throw new ApiError("IDENTITY_UNKNOWN", "工作区访问方式信息不一致。");
  return user;
}
export const teamApi = {
  async me(signal?: AbortSignal): Promise<TeamUser> {
    return validateTeamUser(await teamRequest<TeamUser>("/design-auth/me", { signal }));
  },
  async demoAccess(role: TeamUser["role"], signal?: AbortSignal): Promise<TeamUser> {
    const user = validateTeamUser(await teamRequest<TeamUser>("/design-auth/demo-access", { ...jsonBody({ role }), signal }));
    if (user.access_mode !== "demo" || user.role !== role) throw new ApiError("IDENTITY_UNKNOWN", "尚未确认所选演示角色，请重新连接。");
    return user;
  },
  login: (username: string, password: string) => teamRequest<TeamUser>("/design-auth/login", jsonBody({ username, password })),
  logout: () => teamRequest<{ ok: boolean }>("/design-auth/logout", jsonBody({})),
  staff: (signal?: AbortSignal) => teamRequest<{ items: TeamMember[] }>("/design-auth/staff", { signal }),
};
export const errorText = (error: unknown) => error instanceof Error ? error.message : "操作未完成，请重试。";
export const isAbort = (error: unknown) => error instanceof DOMException && error.name === "AbortError";
