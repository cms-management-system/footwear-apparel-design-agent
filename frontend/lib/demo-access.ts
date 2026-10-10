import { ApiError, isAbort, teamApi, type TeamUser } from "./team-api";
import { appPath } from "./app-path";

export const demoRole = (value: string | null): TeamUser["role"] | null => value === "designer" || value === "manager" ? value : null;
export function demoDestination(role: TeamUser["role"]) { return role === "designer" ? "/?demo_role=designer" : "/handoffs?demo_role=manager"; }
export function navigateDemo(role: TeamUser["role"]) { window.location.replace(appPath(demoDestination(role))); }
export function navigateDemoEntry(role: TeamUser["role"]) { window.location.replace(appPath(`/demo/${role}`)); }
export function navigateTeamWorkspace(role: TeamUser["role"]) { window.location.replace(appPath(role === "designer" ? "/" : "/handoffs")); }
/** Bootstrap only after server confirms demo mode. Unknown bootstrap is read back, never resent. */
export async function resolveDemoRole(role: TeamUser["role"], signal?: AbortSignal): Promise<TeamUser> {
  const current = await teamApi.me(signal);
  if (current.access_mode !== "demo") throw new ApiError("DEMO_ACCESS_DISABLED", "演示入口尚未启用，请使用团队登录入口。", 404);
  if (signal?.aborted) throw new DOMException("Request cancelled", "AbortError");
  try { await teamApi.demoAccess(role, signal); }
  catch (cause) {
    if (isAbort(cause) || signal?.aborted) throw cause;
    if (!(cause instanceof ApiError) || !["RESPONSE_UNKNOWN", "NETWORK_UNAVAILABLE", "IDENTITY_UNKNOWN", "INVALID_RESPONSE"].includes(cause.code)) throw cause;
    // A lost identity receipt has no business action to retry. Read the server's actual role.
  }
  const confirmed = await teamApi.me(signal);
  if (signal?.aborted) throw new DOMException("Request cancelled", "AbortError");
  if (confirmed.access_mode !== "demo" || confirmed.role !== role) throw new ApiError("DEMO_ROLE_UNCONFIRMED", "角色切换尚未确认，请重新进入演示入口。", 409);
  return confirmed;
}
