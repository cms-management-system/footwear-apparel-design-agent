import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TeamBoundary } from "@/components/team/TeamSession";
import PlatformBar from "@/components/design/PlatformBar";
import ProjectWorkspace from "@/components/workspace/ProjectWorkspace";
import { agentApi, type Workspace } from "@/lib/agent-api";
import { bindDesignContext, teamApi, type TeamUser } from "@/lib/team-api";
import { navigateTeamWorkspace } from "@/lib/demo-access";
const route = vi.hoisted(() => ({ pathname: "/v2/design" }));
vi.mock("next/navigation", () => ({ usePathname: () => route.pathname }));
vi.mock("@/lib/demo-access", async original => ({ ...await original<typeof import("@/lib/demo-access")>(), navigateTeamWorkspace: vi.fn() }));
vi.mock("@/lib/team-api", async original => { const real = await original<typeof import("@/lib/team-api")>(); return { ...real, teamApi: { ...real.teamApi, me: vi.fn() } }; });
vi.mock("@/lib/agent-api", async original => { const real = await original<typeof import("@/lib/agent-api")>(); return { ...real, agentApi: { ...real.agentApi, workspace: vi.fn(), capabilities: vi.fn() } }; });
vi.mock("@/components/workspace/useCanvas", () => ({ useCanvas: () => ({ dirty: false, state: "clean", pending: null, remote: null, error: "" }) }));
vi.mock("@/components/workspace/DesignCanvas", () => ({ default: () => <div>合成私有画布</div> }));
vi.mock("@/components/workspace/ProjectConversation", () => ({ default: () => <div>合成对话</div> }));
const manager: TeamUser = { username: "public-demo-manager", display_name: "演示设计经理", role: "manager", integration_mode: "managed", auth_context_id: "public-context", instance_id: "design-public-20261010", scope_id: "public-three-agent-20261010", access_mode: "demo", demo_entry_urls: { designer: "/demo/designer", manager: "/demo/manager" } };
class Stream {
  static items: Stream[] = []; closed = false; onerror: (() => void) | null = null;
  constructor(public url: string) { Stream.items.push(this); }
  addEventListener() {}
  close() { this.closed = true; }
}
beforeEach(() => { vi.stubEnv("NEXT_PUBLIC_BASE_PATH", "/v2/design"); vi.clearAllMocks(); route.pathname = "/v2/design"; vi.mocked(teamApi.me).mockResolvedValue(manager); bindDesignContext(null); Stream.items = []; });
afterEach(() => { cleanup(); bindDesignContext(null); vi.unstubAllGlobals(); vi.unstubAllEnvs(); window.history.replaceState(null, "", "/"); });
it.each(["/v2/design", "/v2/design/"])("经理普通首页%s恢复工作台而不创建项目或重新选择角色", async path => {
  window.history.replaceState(null, "", path); render(<TeamBoundary>{u => <div>{u.display_name}</div>}</TeamBoundary>);
  await waitFor(() => expect(navigateTeamWorkspace).toHaveBeenCalledWith("manager")); expect(teamApi.me).toHaveBeenCalledTimes(1);
});
it("导航识别带prefix的项目画布与任务/设置，保持原隐藏顶栏与当前项", () => {
  route.pathname = "/v2/design/projects/7"; const view = render(<PlatformBar />);
  expect(document.querySelector(".platformBar")?.hasAttribute("hidden")).toBe(true);
  route.pathname = "/v2/design/handoffs"; view.rerender(<PlatformBar />);
  expect(screen.getByRole("link", { name: "任务" }).getAttribute("aria-current")).toBe("page");
  route.pathname = "/settings"; view.rerender(<PlatformBar />); expect(screen.getByRole("link", { name: "设置" }).getAttribute("aria-current")).toBe("page");
});
it("实际项目SSE使用设计prefix，卸载关闭流，不在根/api重连", async () => {
  vi.stubGlobal("EventSource", Stream); const user = { ...manager, role: "designer" as const }; bindDesignContext(user.auth_context_id!, user);
  const workspace: Workspace = { project: { id: 77, name: "合成公网项目" }, head: {}, revision: 1, specs: [], assets: [], versions: [], tasks: [], messages: [] };
  vi.mocked(agentApi.workspace).mockResolvedValue(workspace); vi.mocked(agentApi.capabilities).mockRejectedValue(new Error("offline"));
  const view = render(<ProjectWorkspace projectId={77} user={user} />); await screen.findByText("合成私有画布");
  expect(Stream.items).toHaveLength(1); expect(Stream.items[0].url).toBe("/v2/design/api/project/77/design-events");
  await act(async () => view.unmount()); expect(Stream.items[0].closed).toBe(true);
});
