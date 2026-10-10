import { storageOwner, teamRequest, type TeamUser, ApiError } from "./team-api";
import { executeAction, freezeAction, type FrozenAction } from "./design-workflow";
import type { DesignPrompt } from "./agent-api";
export type CreateDesignInput = { title: string; design_object: string; initial_prompt: string; output_kind: "effect_image" | "design_draft" };
export type CreateDesignResult = { project_id: number; source_mode: "independent"; owner_subject: string; scope_id: string; revision: number; prompt: DesignPrompt; allowed_actions: string[] };
export const createPendingKey = (user: TeamUser) => `design-create-pending:${storageOwner(user)}`;
export function createDesignAction(user: TeamUser, value: CreateDesignInput) { return freezeAction(user, "/design-projects", value); }
export function saveCreateAction(user: TeamUser, action: FrozenAction | null) {
  try { if (action) sessionStorage.setItem(createPendingKey(user), JSON.stringify(action)); else sessionStorage.removeItem(createPendingKey(user)); } catch { /* The page retains its in-memory request. */ }
}
export function loadCreateAction(user: TeamUser): FrozenAction | null {
  try {
    const a = JSON.parse(sessionStorage.getItem(createPendingKey(user)) ?? "null") as FrozenAction | null;
    if (!a || a.path !== "/design-projects" || a.method !== "POST" || a.actor !== user.username || a.scope !== user.scope_id || a.instance !== user.instance_id || !/^[A-Za-z0-9_-]{8,100}$/.test(a.id) || typeof a.body !== "string" || typeof a.context !== "string") return null;
    return a;
  } catch { return null; }
}
function verifyCreated(result: CreateDesignResult, user: TeamUser) {
  if (!result || typeof result !== "object" || !Number.isSafeInteger(result.project_id) || result.project_id <= 0 || result.source_mode !== "independent" || result.owner_subject !== user.username || result.scope_id !== user.scope_id || result.revision !== 1) throw new ApiError("RESPONSE_UNKNOWN", "新项目归属尚未完整确认，请先查询原创建操作。");
  return result;
}
export async function executeCreateAction(action: FrozenAction, user: TeamUser) { return verifyCreated(await executeAction<CreateDesignResult>(action, user), user); }
export async function queryCreateAction(action: FrozenAction, user: TeamUser) {
  const operation = await teamRequest<{ action_id: string; method: string; path: string; status_code: number; result: CreateDesignResult }>(`/design-operations/${encodeURIComponent(action.id)}`);
  if (operation.action_id !== action.id || operation.method !== "POST" || operation.path !== "/api/design-projects" || operation.status_code !== 201) throw new ApiError("RESPONSE_UNKNOWN", "原创建操作尚未确认成功，请保留请求并核对。");
  return verifyCreated(operation.result, user);
}
