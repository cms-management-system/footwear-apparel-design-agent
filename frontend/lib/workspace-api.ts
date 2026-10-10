import { storageOwner, ApiError, currentDesignUser, designContextKey, jsonBody, teamRequest, type TeamUser } from "./team-api";
import { engineRequest } from "./engine-api";
import type { ChatMessage, DesignPrompt } from "./agent-api";

export type SelectedContext = { version_id: string | null; asset_id: string | null };
export type CanvasNode = { id: string; kind: "version" | "asset"; ref_id: string; x: number; y: number; width: number; height: number };
export type CanvasLayout = { nodes: CanvasNode[]; viewport: { x: number; y: number; zoom: number } };
export type CanvasDocument = { schema_version: "design-canvas/1"; project_id: number; layout_revision: number; layout: CanvasLayout; updated_at: string | null; updated_by: string | null };
export type CanvasResult = CanvasDocument & { action_id: string; base_layout_revision: number };
export type BriefInput = { expected_prompt_id: string | null; expected_spec_id: string | null; message_id: string | null; selected_context: SelectedContext | null; positive_prompt: string; avoid_items: string[]; output_kind: "effect_image" | "design_draft"; base_version_id: string | null; edit_region: string };
export type Brief = Omit<BriefInput, "expected_prompt_id" | "expected_spec_id"> & { id: string; project_id: number; origin: "manual" | "model"; model_task_id: string | null; based_on_latest_message_id: string | null; based_on_prompt_id: string | null; based_on_spec_id: string | null; source_binding_digest: string | null; created_by: string; created_at: string; digest: string; status: "candidate" | "confirmed" };
export type MessageInput = { intent?: "auto" | "discuss" | "generate_image"; output_kind?: "effect_image" | "design_draft" | null; text: string; references: []; expected_prompt_id: string | null; expected_spec_id: string | null; selected_context: SelectedContext | null; idempotency_key: string; authorized: boolean };
export type MessageResult = ChatMessage & { intent_id?: string | null; run_id?: string | null; reply_state?: string; reason?: string; task_id?: string | null; brief_id?: string | null };
export type LayoutAction = { id: string; projectId: number; context: string; owner: string; body: string; base: number };
const ownerKey = (u: TeamUser) => storageOwner(u);
export const canvasStorageKey = (pid: number, u: TeamUser) => `design-canvas-pending:${ownerKey(u)}:${pid}`;
const memory = new Map<string, LayoutAction>();
const locks = new Set<string>();
function store(action: LayoutAction | null, pid: number, u: TeamUser) {
  const key = canvasStorageKey(pid, u);
  if (action) memory.set(key, action); else memory.delete(key);
  try { if (action) sessionStorage.setItem(key, JSON.stringify(action)); else sessionStorage.removeItem(key); } catch { /* In-memory recovery remains available. */ }
}
function retireLayout(action: LayoutAction, u: TeamUser) {
  const key = canvasStorageKey(action.projectId, u);
  if (memory.get(key) && memory.get(key)?.id !== action.id) return;
  try { const stored = JSON.parse(sessionStorage.getItem(key) ?? "null"); if (stored && stored.id !== action.id) return; } catch { /* In-memory ownership still applies. */ }
  if (pendingLayout(action.projectId, u)?.id === action.id) store(null, action.projectId, u);
}
export function pendingLayout(pid: number, u: TeamUser): LayoutAction | null {
  const key = canvasStorageKey(pid, u);
  const cached = memory.get(key); if (cached) return cached;
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null") as LayoutAction | null;
    if (!value || value.owner !== ownerKey(u) || value.projectId !== pid || !/^[A-Za-z0-9_-]{8,100}$/.test(value.id) || typeof value.context !== "string" || typeof value.body !== "string" || !Number.isSafeInteger(value.base) || value.base < 0) return null;
    const body = JSON.parse(value.body);
    if (body.schema_version !== "design-canvas/1" || body.expected_layout_revision !== value.base || !Array.isArray(body.layout?.nodes)) return null;
    memory.set(key, value); return value;
  } catch { return null; }
}
export function freezeLayout(pid: number, doc: CanvasDocument, layout: CanvasLayout, u: TeamUser): LayoutAction {
  if (pendingLayout(pid, u)) throw new ApiError("RECOVERY_REQUIRED", "原布局保存待核对，请先查询原操作。");
  const action: LayoutAction = { id: crypto.randomUUID(), projectId: pid, context: u.auth_context_id ?? "", owner: ownerKey(u), base: doc.layout_revision, body: JSON.stringify({ schema_version: "design-canvas/1", expected_layout_revision: doc.layout_revision, layout }) };
  store(action, pid, u); return action;
}
function validate(doc: CanvasDocument, pid: number) {
  if (doc?.schema_version !== "design-canvas/1" || doc.project_id !== pid || !Number.isSafeInteger(doc.layout_revision) || doc.layout_revision < 0 || !Array.isArray(doc.layout?.nodes) || !doc.layout.viewport || !Number.isFinite(doc.layout.viewport.zoom)) throw new ApiError("INVALID_RESPONSE", "画布格式尚未确认，请重新读取。");
  return doc;
}
function verifyResult(result: CanvasResult, action: LayoutAction) {
  validate(result, action.projectId);
  if (result.action_id !== action.id || result.base_layout_revision !== action.base || result.layout_revision !== action.base + 1) throw new ApiError("RESPONSE_UNKNOWN", "布局保存回执待核对，请查询原操作。");
  return result;
}
export async function writeLayout(action: LayoutAction, u: TeamUser) {
  if (action.owner !== ownerKey(u) || currentDesignUser() !== u || action.context !== designContextKey()) throw new ApiError("AUTH_CONTEXT_CHANGED", "会话已变化，原布局操作只能查询。", 409);
  const key = canvasStorageKey(action.projectId, u);
  if (locks.has(key)) throw new ApiError("ACTION_IN_PROGRESS", "正在保存原布局。");
  locks.add(key);
  try {
    const result = verifyResult(await teamRequest<CanvasResult>(`/project/${action.projectId}/design-canvas`, { method: "PUT", body: action.body, headers: { "Content-Type": "application/json", "X-Design-Context": action.context, "X-Design-Action-Id": action.id } }), action);
    if (currentDesignUser() === u && designContextKey() === action.context) retireLayout(action, u);
    return result;
  } catch (error) {
    if (error instanceof ApiError && [400, 403, 404, 409, 413, 422].includes(error.status) && error.code !== "AUTH_CONTEXT_CHANGED") retireLayout(action, u);
    throw error;
  } finally { locks.delete(key); }
}
export async function queryLayout(action: LayoutAction, u: TeamUser) {
  if (action.owner !== ownerKey(u) || currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化，请重新确认原布局归属。", 409);
  const operation = await teamRequest<{ action_id: string; method: string; path: string; revision_domain?: string; status_code: number; result: CanvasResult & { revision_domain?: string } }>(`/design-operations/${action.id}`);
  if (currentDesignUser() !== u) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化。", 409);
  if (operation.action_id !== action.id || operation.method !== "PUT" || operation.path !== `/api/project/${action.projectId}/design-canvas` || operation.status_code !== 200 || (operation.revision_domain ?? operation.result?.revision_domain) !== "layout") throw new ApiError("RESPONSE_UNKNOWN", "原布局回执尚未确认，请保留草稿。");
  const result = verifyResult(operation.result, action); retireLayout(action, u); return result;
}
export const workspaceApi = {
  canvas: async (pid: number) => { const context = designContextKey(); const value = await teamRequest<CanvasDocument>(`/project/${pid}/design-canvas`); if (context !== designContextKey()) throw new ApiError("AUTH_CONTEXT_CHANGED", "账号已变化。", 409); return validate(value, pid); },
  message: (pid: number, input: MessageInput) => engineRequest<MessageResult>(`/project/${pid}/design-messages`, { ...jsonBody(input), headers: { "Content-Type": "application/json", "X-Design-Action-Id": input.idempotency_key } }),
  brief: (pid: number, input: BriefInput) => engineRequest<{ brief: Brief; revision: number }>(`/project/${pid}/design-briefs`, jsonBody(input)),
  confirm: (pid: number, input: { candidate_id: string | null; expected_spec_id: string | null; expected_prompt_id: string | null; positive_prompt: string; avoid_items: string[]; output_kind: "effect_image" | "design_draft"; base_version_id: string | null; edit_region: string }) => engineRequest<{ prompt: DesignPrompt; spec_id: string; source_brief_id: string | null; revision: number }>(`/project/${pid}/design-prompts`, jsonBody(input)),
};
