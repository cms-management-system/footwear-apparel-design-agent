import { storageOwner, ApiError, currentDesignUser, teamRequest, type TeamUser } from "./team-api";
import { canReplay, executeAction, freezeAction, type FrozenAction } from "./design-workflow";

export type DirectInput = { entry_mode: "idea" | "blank"; text: string; output_kind: "effect_image" | "design_draft"; intent: "generate_image" | "discuss" | "none"; authorized: boolean };
export type DirectResult = { project_id: number; source_mode: "independent"; revision: number; action_id: string; base_revision: number; message_id: string | null; intent_id: string | null; run_id: string | null; run_status: string | null; url: string; reason?: string | null };
export const directKey = (u: TeamUser) => `design-direct-pending:${storageOwner(u)}`;
const memory = new Map<string, FrozenAction>();
const locks = new Set<string>();
export function loadDirect(u: TeamUser): FrozenAction | null {
  const key = directKey(u); const cached = memory.get(key); if (cached) return cached;
  try {
    const a = JSON.parse(sessionStorage.getItem(key) ?? "null") as FrozenAction | null;
    if (!a || a.path !== "/design-projects/direct" || a.method !== "POST" || a.actor !== u.username || a.scope !== u.scope_id || a.instance !== u.instance_id || !/^[A-Za-z0-9_-]{8,100}$/.test(a.id) || typeof a.body !== "string" || typeof a.context !== "string") return null;
    memory.set(key, a); return a;
  } catch { return null; }
}
export function retireDirect(a: FrozenAction, u: TeamUser) {
  if (loadDirect(u)?.id !== a.id) return;
  memory.delete(directKey(u));
  try { if (JSON.parse(sessionStorage.getItem(directKey(u)) ?? "null")?.id === a.id) sessionStorage.removeItem(directKey(u)); } catch { /* Memory preserves ownership. */ }
}
export function freezeDirect(u: TeamUser, input: DirectInput) {
  if (loadDirect(u) || locks.has(directKey(u))) throw new ApiError("RECOVERY_REQUIRED", "原创建操作尚待核对，请先查询原记录。");
  if (input.text.length > 4000 || (input.entry_mode === "idea" && !input.text.trim())) throw new ApiError("INVALID_INPUT", "请输入不超过4000字的设计想法。");
  const a = freezeAction(u, "/design-projects/direct", input); memory.set(directKey(u), a);
  try { sessionStorage.setItem(directKey(u), JSON.stringify(a)); } catch { /* Original request remains in memory. */ }
  return a;
}
function verify(result: DirectResult, a: FrozenAction) {
  const input = JSON.parse(a.body) as DirectInput;
  const ids = [result?.message_id, result?.intent_id, result?.run_id];
  if (!result || !Number.isSafeInteger(result.project_id) || result.project_id < 1 || result.source_mode !== "independent" || result.action_id !== a.id || result.base_revision !== 0 || result.revision !== 1 || result.url !== `/projects/${result.project_id}` || (input.entry_mode === "blank" ? ids.some(v => v !== null) || result.run_status !== null : ids.some(v => typeof v !== "string" || !v) || !["queued", "blocked"].includes(result.run_status ?? ""))) throw new ApiError("RESPONSE_UNKNOWN", "创建回执尚未完整确认，请查询原创建操作。");
  return result;
}
export async function executeDirect(a: FrozenAction, u: TeamUser) {
  const key = directKey(u);
  if (locks.has(key)) throw new ApiError("ACTION_IN_PROGRESS", "原创建操作正在返回，请稍候。");
  locks.add(key);
  try {
    const result = verify(await executeAction<DirectResult>(a, u), a);
    if (currentDesignUser() !== u || !canReplay(a, u)) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请在原账号查询创建记录。", 409);
    retireDirect(a, u); return result;
  } catch (cause) {
    if (cause instanceof ApiError && [400, 403, 404, 409, 410, 413, 422].includes(cause.status) && cause.code !== "AUTH_CONTEXT_CHANGED") retireDirect(a, u);
    throw cause;
  } finally { locks.delete(key); }
}
export async function queryDirect(a: FrozenAction, u: TeamUser) {
  if (currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "请在原账号查询创建记录。", 409);
  const operation = await teamRequest<{ action_id: string; method: string; path: string; status_code: number; result: DirectResult }>(`/design-operations/${encodeURIComponent(a.id)}`);
  if (currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请重新查询。", 409);
  if (operation.action_id !== a.id || operation.method !== "POST" || operation.path !== "/api/design-projects/direct" || operation.status_code !== 201) throw new ApiError("RESPONSE_UNKNOWN", "原创建记录尚未确认成功，请保留原文并核对。");
  const result = verify(operation.result, a); retireDirect(a, u); return result;
}
