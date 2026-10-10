import { storageOwner, ApiError, currentDesignUser, designContextKey, teamRequest, type TeamUser } from "./team-api";
import type { Workspace } from "./agent-api";

export const ENGINE_RECOVERY_EVENT = "design-engine-recovery";
type EngineResult = { id?: string; task_id?: string; action_id?: string; base_revision?: number; revision?: number };
export type EngineAction = { id: string; projectId: number; path: string; method: string; revision: number; context: string; owner: string; body: string | null; fingerprint: string; contentType: string; created_at: string };
const revisions = new Map<string, number>();
const records = new Map<string, number>();
const files = new Map<string, Blob>();
const actions = new Map<string, EngineAction>();
const locks = new Set<string>();
const owner = (user: TeamUser) => storageOwner(user);
const projectKey = (projectId: number) => `${designContextKey()}:${projectId}`;
const storageKey = (projectId: number, user: TeamUser) => `design-engine-pending:${owner(user)}:${projectId}`;
function changed() { if (typeof window !== "undefined") window.dispatchEvent(new Event(ENGINE_RECOVERY_EVENT)); }
export function rememberWorkspace(workspace: Workspace) {
  const pid = workspace.project.id;
  workspace.tasks = workspace.tasks.map(task => Object.assign({ steps: [], observations: [], questions: [], outcome: null, reasoning_calls: 0, image_calls: 0, reserved_cost_fen: 0, max_cost_fen: null }, task));
  if (Number.isSafeInteger(workspace.revision) && (workspace.revision ?? 0) > 0) revisions.set(projectKey(pid), Math.max(workspace.revision!, revisions.get(projectKey(pid)) ?? 0));
  for (const collection of [workspace.assets, workspace.specs, workspace.tasks, workspace.versions, workspace.style_plans, workspace.models3d, workspace.sampling_sheets, workspace.technical_flats, workspace.prompts]) {
    for (const item of collection ?? []) records.set(`${designContextKey()}:${item.id}`, pid);
  }
}
function pathProject(path: string): number | undefined {
  const project = /^\/project\/(\d+)\//.exec(path);
  if (project) return Number(project[1]);
  const record = /^\/(?:design-assets|design-specs|design-tasks|design-versions|style-plans|technical-flats|design-models3d)\/([^/?]+)\//.exec(path);
  return record ? records.get(`${designContextKey()}:${record[1]}`) : undefined;
}
export function pendingEngineAction(projectId: number): EngineAction | null {
  const user = currentDesignUser(); if (!user) return null;
  const key = storageKey(projectId, user);
  const cached = actions.get(key); if (cached) return cached;
  try {
    const action = JSON.parse(sessionStorage.getItem(key) ?? "null") as EngineAction | null;
    if (!action || action.owner !== owner(user) || action.projectId !== projectId || !/^[A-Za-z0-9_-]{8,100}$/.test(action.id) || !["POST", "PATCH", "DELETE"].includes(action.method) || typeof action.context !== "string" || !Number.isSafeInteger(action.revision) || action.revision < 1 || typeof action.path !== "string" || !action.path.startsWith("/") || action.path.includes("..") || !/^\/(?:project\/\d+|design-assets\/[^/?]+|design-specs\/[^/?]+|design-tasks\/[^/?]+|design-versions\/[^/?]+|style-plans\/[^/?]+|technical-flats\/[^/?]+|design-models3d\/[^/?]+)\//.test(action.path) || (action.body !== null && typeof action.body !== "string") || typeof action.fingerprint !== "string" || typeof action.contentType !== "string") return null;
    actions.set(key, action); return action;
  } catch { return null; }
}
function persist(action: EngineAction | null, projectId: number) {
  const user = currentDesignUser(); if (!user) return;
  const key = storageKey(projectId, user);
  if (action) actions.set(key, action); else actions.delete(key);
  try { if (action) sessionStorage.setItem(key, JSON.stringify(action)); else sessionStorage.removeItem(key); } catch { /* Memory preserves recovery if browser storage is unavailable. */ }
  changed();
}
/** A late result may retire only its original owner's exact action, never the active account's queue. */
function retire(action: EngineAction) {
  const key = `design-engine-pending:${action.owner}:${action.projectId}`;
  const cached = actions.get(key);
  if (cached && cached.id !== action.id) return;
  try {
    const stored = JSON.parse(sessionStorage.getItem(key) ?? "null") as EngineAction | null;
    if (stored && (stored.id !== action.id || stored.owner !== action.owner || stored.projectId !== action.projectId)) return;
    if (stored?.id === action.id) sessionStorage.removeItem(key);
  } catch { /* Memory is sufficient when session storage is unavailable. */ }
  if (cached?.id === action.id) actions.delete(key);
  changed();
}
function adopt(result: EngineResult, projectId: number) {
  if (Number.isSafeInteger(result.revision) && (result.revision ?? 0) > 0) revisions.set(projectKey(projectId), Math.max(result.revision!, revisions.get(projectKey(projectId)) ?? 0));
  if (typeof result.id === "string") records.set(`${designContextKey()}:${result.id}`, projectId);
  if (typeof result.task_id === "string") records.set(`${designContextKey()}:${result.task_id}`, projectId);
}
async function fingerprintBody(body: BodyInit | null | undefined): Promise<string> {
  if (body instanceof Blob) return `blob:${Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await body.arrayBuffer()))).map(byte => byte.toString(16).padStart(2, "0")).join("")}`;
  return `text:${typeof body === "string" ? body : ""}`;
}
export function canRetryEngine(action: EngineAction) { return action.context === designContextKey() && (action.body !== null || files.has(action.id)); }
async function run<T>(action: EngineAction, suppliedBody?: BodyInit | null, timeoutMs = 30000): Promise<T> {
  const user = currentDesignUser();
  if (!user || action.owner !== owner(user) || action.context !== user.auth_context_id) throw new ApiError("AUTH_CONTEXT_CHANGED", "原操作属于之前的会话，当前只能查询原记录。", 409);
  if (pathProject(action.path) !== action.projectId) throw new ApiError("ORIGINAL_TARGET_UNKNOWN", "请先读取对应项目，再核对原操作目标。可以继续只读查询。");
  const key = storageKey(action.projectId, user);
  if (locks.has(key)) throw new ApiError("ACTION_IN_PROGRESS", "原操作正在处理，请先等待结果。");
  const body = suppliedBody ?? action.body ?? files.get(action.id);
  if (body === undefined) throw new ApiError("ORIGINAL_FILE_REQUIRED", "请重新选择原始素材后重试；不能以新素材覆盖原上传。");
  locks.add(key);
  try {
    const result = await teamRequest<T & EngineResult>(action.path, { method: action.method, body, headers: { "Content-Type": action.contentType, "X-Design-Context": action.context, "X-Design-Action-Id": action.id, "X-Design-Revision": String(action.revision) } }, timeoutMs);
    if (result.action_id !== action.id || result.base_revision !== action.revision || !Number.isSafeInteger(result.revision) || (result.revision ?? 0) <= action.revision) throw new ApiError("RESPONSE_UNKNOWN", "未收到完整操作凭据，请先查询原操作。");
    if (currentDesignUser() && owner(currentDesignUser()!) === action.owner && designContextKey() === action.context) { adopt(result, action.projectId); retire(action); files.delete(action.id); }
    return result;
  } catch (cause) {
    if (cause instanceof ApiError && [400, 403, 404, 409, 410, 413, 422].includes(cause.status) && cause.code !== "AUTH_CONTEXT_CHANGED") retire(action);
    throw cause;
  } finally { locks.delete(key); }
}
export async function engineRequest<T>(path: string, init: RequestInit = {}, timeoutMs = 30000): Promise<T> {
  const writing = !["GET", "HEAD"].includes((init.method ?? "GET").toUpperCase());
  const user = currentDesignUser();
  if (!writing || user?.integration_mode !== "managed" || path === "/project") return teamRequest<T>(path, init, timeoutMs);
  const projectId = pathProject(path);
  if (!projectId) throw new ApiError("REVISION_REQUIRED", "请先重新读取对应设计项目，再执行操作。");
  const revision = revisions.get(projectKey(projectId));
  if (!revision) throw new ApiError("REVISION_REQUIRED", "尚未取得当前设计修订，请重新读取工作区。");
  const context = user.auth_context_id;
  if (!context) throw new ApiError("CONTEXT_REQUIRED", "请先重新确认登录身份。");
  const fingerprint = await fingerprintBody(init.body);
  if (currentDesignUser() !== user || context !== designContextKey()) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请重新打开当前设计项目。", 409);
  const existing = pendingEngineAction(projectId);
  if (existing) {
    if (existing.path !== path || existing.fingerprint !== fingerprint || existing.method !== (init.method ?? "POST") || existing.context !== context) throw new ApiError("RECOVERY_REQUIRED", "原操作结果仍待核对。你的新编辑已保留，请先查询原记录或重试原请求。");
    return run<T>(existing, init.body, timeoutMs);
  }
  if (locks.has(storageKey(projectId, user))) throw new ApiError("ACTION_IN_PROGRESS", "原操作的响应仍在返回，请等待后再保存新编辑。");
  const action: EngineAction = { id: new Headers(init.headers).get("X-Design-Action-Id") || crypto.randomUUID(), projectId, path, method: init.method ?? "POST", revision, context, owner: owner(user), body: typeof init.body === "string" ? init.body : init.body ? null : "", fingerprint, contentType: new Headers(init.headers).get("Content-Type") ?? "application/json", created_at: new Date().toISOString() };
  if (init.body instanceof Blob) files.set(action.id, init.body);
  persist(action, projectId);
  return run<T>(action, init.body, timeoutMs);
}
export async function retryEngineAction(projectId: number) {
  const action = pendingEngineAction(projectId); if (!action) throw new ApiError("NOT_FOUND", "没有待恢复的原操作。");
  return run(action);
}
export async function queryEngineAction(projectId: number) {
  const action = pendingEngineAction(projectId); if (!action) throw new ApiError("NOT_FOUND", "没有待查询的原操作。");
  const user = currentDesignUser();
  const operation = await teamRequest<{ action_id: string; method: string; status_code: number; path: string; result: EngineResult }>(`/design-operations/${encodeURIComponent(action.id)}`);
  if (!user || currentDesignUser() !== user) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请重新查询。", 409);
  if (operation.action_id !== action.id || operation.method !== action.method || operation.path !== `/api${action.path}` || operation.status_code < 200 || operation.status_code >= 300 || operation.result?.action_id !== action.id || operation.result.base_revision !== action.revision || !Number.isSafeInteger(operation.result.revision) || (operation.result.revision ?? 0) <= action.revision) throw new ApiError("RESPONSE_UNKNOWN", "原操作记录尚未完整确认，请保留原请求并重新核对。");
  adopt(operation.result, projectId); retire(action); files.delete(action.id);
  return operation.result;
}
