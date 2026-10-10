import { storageOwner, teamRequest, ApiError, type TeamUser } from "./team-api";

export type Handoff = {
  id: string; package_id: string; version: string; package: Record<string, unknown>;
  status: string; assignee: string | null; project_id: number | null;
  submitted_version_id: string | null; manager_note: string; created_at: string;
  revision: number; allowed_actions: string[];
  source_receipt?: Record<string, unknown> | null;
  source_binding?: { package?: Record<string, unknown>; requirement?: unknown; prompt?: unknown; receipt?: Record<string, unknown> | null } | null;
  submission?: Record<string, unknown> | null;
  review?: Record<string, unknown> | null;
  delivery?: Record<string, unknown> | null;
  priority?: string; due_date?: string | null;
  history?: Record<string, unknown>[];
  audit?: Record<string, unknown>[];
  deliveries?: Record<string, unknown>[];
};
export type FrozenAction = { id: string; path: string; method: string; body: string; context: string; actor: string; scope: string; instance: string; created_at: string };
export const workflowStatus: Record<string, string> = { new: "待接收判断", accepted: "待分派", returned: "待产品澄清", assigned: "设计中", review: "待管理审查", approved: "设计已批准" };
export const deliveryStatus: Record<string, string> = { not_prepared: "尚未准备回传", prepared: "待发送", sending: "正在发送", unconfirmed: "送达结果待确认", received: "产品已接收", failed_before_send: "发送前失败" };
export const label = (value: string, source = workflowStatus) => source[value] ?? `待核对（${value || "未知"}）`;
export const textField = (record: Record<string, unknown> | null | undefined, key: string, fallback = "未提供") => typeof record?.[key] === "string" && record[key] ? record[key] as string : fallback;
export function titleOf(item: Handoff) { return textField(item.package, "title", textField(item.package, "requirement_desc", item.package_id)); }
export const workflowApi = {
  list: (offset: number, signal?: AbortSignal) => teamRequest<{ items: Handoff[]; total: number }>(`/design-handoffs?offset=${offset}&limit=20`, { signal }),
  detail: (id: string, signal?: AbortSignal) => teamRequest<Handoff>(`/design-handoffs/${encodeURIComponent(id)}`, { signal }),
  operation: (id: string) => teamRequest<Record<string, unknown>>(`/design-operations/${encodeURIComponent(id)}`),
};
export function freezeAction(user: TeamUser, path: string, value: unknown, method = "POST"): FrozenAction {
  if (!user.auth_context_id || !user.scope_id || !user.instance_id) throw new ApiError("CONTEXT_REQUIRED", "尚未取得完整工作区身份，请重新连接。");
  return { id: crypto.randomUUID(), path, method, body: JSON.stringify(value), context: user.auth_context_id, actor: user.username, scope: user.scope_id, instance: user.instance_id, created_at: new Date().toISOString() };
}
export function canReplay(action: FrozenAction, user: TeamUser) {
  return action.context === user.auth_context_id && action.actor === user.username && action.scope === user.scope_id && action.instance === user.instance_id;
}
export async function executeAction<T>(action: FrozenAction, user: TeamUser): Promise<T> {
  if (!canReplay(action, user)) throw new ApiError("AUTH_CONTEXT_CHANGED", "这是之前会话的操作，只能查询原记录，不能以当前会话重发。");
  return teamRequest<T>(action.path, { method: action.method, headers: { "Content-Type": "application/json", "X-Design-Context": action.context, "X-Design-Action-Id": action.id }, body: action.body });
}
export function pendingKey(user: TeamUser, handoff: string) { return user.access_mode === "demo" ? `design-pending:${storageOwner(user)}:${encodeURIComponent(handoff)}` : `design-pending:${user.instance_id}:${user.scope_id}:${user.username}:${encodeURIComponent(handoff)}`; }
export function loadPending(user: TeamUser, handoff: string): FrozenAction | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(pendingKey(user, handoff)) ?? "null");
    if (!value || typeof value !== "object") return null;
    const a = value as FrozenAction;
    if (a.actor !== user.username || a.scope !== user.scope_id || a.instance !== user.instance_id || typeof a.id !== "string" || typeof a.context !== "string" || typeof a.body !== "string" || a.method !== "POST") return null;
    if (!["decision", "submit", "review", "delivery"].some(endpoint => a.path === `/design-handoffs/${encodeURIComponent(handoff)}/${endpoint}`) || !/^[A-Za-z0-9_-]{8,100}$/.test(a.id)) return null;
    return a;
  } catch { return null; }
}
export function savePending(user: TeamUser, handoff: string, action: FrozenAction | null) {
  try { if (action) sessionStorage.setItem(pendingKey(user, handoff), JSON.stringify(action)); else sessionStorage.removeItem(pendingKey(user, handoff)); } catch { /* In-memory recovery remains available if storage is blocked. */ }
}
