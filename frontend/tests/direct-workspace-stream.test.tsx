import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import ProjectWorkspace from "@/components/workspace/ProjectWorkspace";
import { agentApi, type Workspace } from "@/lib/agent-api";
import { ApiError, bindDesignContext, type TeamUser } from "@/lib/team-api";
vi.mock("@/lib/agent-api", async orig => { const real = await orig<typeof import("@/lib/agent-api")>(); return { ...real, agentApi: { ...real.agentApi, workspace: vi.fn(), capabilities: vi.fn() } }; });
vi.mock("@/components/workspace/useCanvas", () => ({ useCanvas: () => ({ dirty: false, state: "clean", pending: null, remote: null, error: "" }) }));
vi.mock("@/components/workspace/DesignCanvas", () => ({ default: () => <div>private-design-visible</div> }));
vi.mock("@/components/workspace/ProjectConversation", () => ({ default: () => <div>private-chat-visible</div> }));
class Source {
  static items: Source[] = []; closed = false; onerror: (() => void) | null = null;
  constructor() { Source.items.push(this); }
  addEventListener() { /* No artificial workspace event. */ }
  close() { this.closed = true; }
}
const user: TeamUser = { username: "stream-owner", display_name: "合成", role: "designer", integration_mode: "managed", auth_context_id: "stream-ctx", instance_id: "fixture", scope_id: "scope" };
const workspace: Workspace = { project: { id: 92, name: "合成" }, head: {}, revision: 1, specs: [], assets: [], versions: [], tasks: [], messages: [] };
beforeEach(() => { vi.clearAllMocks(); Source.items = []; vi.stubGlobal("EventSource", Source); bindDesignContext(user.auth_context_id!, user); vi.mocked(agentApi.workspace).mockResolvedValue(workspace); vi.mocked(agentApi.capabilities).mockRejectedValue(new Error("offline")); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); bindDesignContext(null); });
it("SSE静默中止后GET发现归档404，立即隐藏私有画布/对话而非永远重连显示旧数据", async () => {
  render(<ProjectWorkspace projectId={92} user={user} />); await screen.findByText("private-design-visible");
  vi.mocked(agentApi.workspace).mockRejectedValueOnce(new ApiError("NOT_FOUND", "项目已移除", 404));
  await act(async () => { Source.items[0].onerror?.(); });
  await waitFor(() => expect(screen.queryByText("private-design-visible")).toBeNull()); expect(screen.queryByText("private-chat-visible")).toBeNull(); expect(agentApi.workspace).toHaveBeenCalledTimes(2); expect(Source.items[0].closed).toBe(true); expect(Source.items).toHaveLength(1);
});
