import { storageOwner, ApiError, currentDesignUser, teamRequest, type TeamUser } from "./team-api";
import { canReplay, freezeAction, type FrozenAction } from "./design-workflow";
export type ProjectAction = FrozenAction & { projectId: number; revision: number };
export type ProjectActionResult = { project_id: number; title?: string; archived: boolean; action_id: string; base_revision: number; revision: number };
export const PROJECT_ACTION_EVENT = "design-project-actions";
const prefix = (u: TeamUser) => `design-project-action:${storageOwner(u)}:`;
const memory = new Map<string, ProjectAction>(); const locks = new Set<string>();
const key = (u: TeamUser, pid: number) => `${prefix(u)}${pid}`;
function changed() { if (typeof window !== "undefined") window.dispatchEvent(new Event(PROJECT_ACTION_EVENT)); }
export function projectActions(u: TeamUser): ProjectAction[] {
  try { for (let i = 0; i < sessionStorage.length; i++) {
    const k = sessionStorage.key(i); if (!k?.startsWith(prefix(u))) continue;
    const a = JSON.parse(sessionStorage.getItem(k) ?? "null") as ProjectAction;
    if (a?.actor === u.username && a.scope === u.scope_id && a.instance === u.instance_id && Number.isSafeInteger(a.projectId) && a.projectId > 0 && Number.isSafeInteger(a.revision) && a.revision > 0 && typeof a.body === "string" && typeof a.context === "string" && /^[A-Za-z0-9_-]{8,100}$/.test(a.id) && ((a.method === "PATCH" && a.path === `/design-projects/${a.projectId}`) || (a.method === "POST" && a.path === `/design-projects/${a.projectId}/archive`)) && k === key(u, a.projectId) && !memory.has(k)) memory.set(k, a);
  } } catch { /* Retain in-memory recovery. */ }
  return [...memory.entries()].filter(([k]) => k.startsWith(prefix(u))).map(([, a]) => a);
}
function retire(a: ProjectAction, u: TeamUser) {
  const k = key(u, a.projectId); if (projectActions(u).find(p => p.projectId === a.projectId)?.id !== a.id) return;
  try { const stored = JSON.parse(sessionStorage.getItem(k) ?? "null"); if (stored && stored.id !== a.id) return; sessionStorage.removeItem(k); } catch { /* Exact in-memory action remains authoritative. */ }
  memory.delete(k); changed();
}
function verify(r: ProjectActionResult, a: ProjectAction) {
  if (r?.project_id !== a.projectId || r.action_id !== a.id || r.base_revision !== a.revision || !Number.isSafeInteger(r.revision) || r.revision <= a.revision || r.archived !== a.path.endsWith("/archive") || (!r.archived && (typeof r.title !== "string" || !r.title.trim()))) throw new ApiError("RESPONSE_UNKNOWN", "项目操作回执尚待核对，请查询原操作。");
  return r;
}
export async function executeProjectAction(a: ProjectAction, u: TeamUser) {
  const k = key(u, a.projectId);
  if (!canReplay(a, u) || currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "会话已变化，当前只能查询原操作。", 409);
  if (locks.has(k)) throw new ApiError("ACTION_IN_PROGRESS", "原操作正在返回，请稍候。"); locks.add(k);
  try {
    const r = verify(await teamRequest<ProjectActionResult>(a.path, { method: a.method, headers: { "Content-Type": "application/json", "X-Design-Context": a.context, "X-Design-Action-Id": a.id, "X-Design-Revision": String(a.revision) }, body: a.body }), a);
    if (currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请在原账号核对结果。", 409);
    retire(a, u); return r;
  } catch (cause) { if (cause instanceof ApiError && [400,403,404,409,410,413,422].includes(cause.status) && cause.code !== "AUTH_CONTEXT_CHANGED") retire(a, u); throw cause; }
  finally { locks.delete(k); }
}
export function freezeProjectAction(pid: number, revision: number, u: TeamUser, title?: string) {
  if (projectActions(u).some(a => a.projectId === pid) || locks.has(key(u, pid))) throw new ApiError("RECOVERY_REQUIRED", "原项目操作待核对，请先查询原记录。");
  if (!Number.isSafeInteger(revision) || revision < 1 || (title !== undefined && (!title.trim() || title.length > 120))) throw new ApiError("INVALID_INPUT", "项目修订或名称无效，请重新读取；名称需1至120字。");
  const a: ProjectAction = { ...freezeAction(u, `/design-projects/${pid}${title === undefined ? "/archive" : ""}`, title === undefined ? { expected_revision: revision, confirm: true } : { expected_revision: revision, title }, title === undefined ? "POST" : "PATCH"), projectId: pid, revision };
  memory.set(key(u, pid), a); try { sessionStorage.setItem(key(u, pid), JSON.stringify(a)); } catch { /* Original action stays in memory. */ } changed(); return a;
}
export async function queryProjectAction(a: ProjectAction, u: TeamUser) {
  if (currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "请在原账号查询原记录。", 409);
  const op = await teamRequest<{ action_id: string; method: string; path: string; status_code: number; result: ProjectActionResult }>(`/design-operations/${encodeURIComponent(a.id)}`);
  if (currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请重新查询。", 409);
  if (op.action_id !== a.id || op.method !== a.method || op.path !== `/api${a.path}` || op.status_code !== 200) throw new ApiError("RESPONSE_UNKNOWN", "原项目操作尚未完整确认，请保留编辑并核对。");
  const result = verify(op.result, a); retire(a, u); return result;
}
