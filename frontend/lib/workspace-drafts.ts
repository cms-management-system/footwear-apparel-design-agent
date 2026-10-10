import { storageOwner, type TeamUser } from "./team-api";
export type ExecutionLocalDraft = { text: string; avoid: string; kind: "effect_image" | "design_draft"; base: string; region: string; expectedPrompt: string | null; expectedSpec: string | null; expectedLatest: string | null };
export function executionDraftKey(user: TeamUser, pid: number) { return `design-execution-draft:${storageOwner(user)}:${pid}`; }
export function readExecutionDraft(user: TeamUser, pid: number): ExecutionLocalDraft | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(executionDraftKey(user, pid)) ?? "null") as ExecutionLocalDraft | null;
    if (!value || ![value.text, value.avoid, value.base, value.region].every(v => typeof v === "string") || !["effect_image", "design_draft"].includes(value.kind) || ![value.expectedPrompt, value.expectedSpec, value.expectedLatest].every(v => v === null || typeof v === "string")) return null;
    return value;
  } catch { return null; }
}
export function storeExecutionDraft(user: TeamUser, pid: number, value: ExecutionLocalDraft | null) {
  try { const key = executionDraftKey(user, pid); if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key); } catch { /* Active editor preserves draft when storage is blocked. */ }
}
