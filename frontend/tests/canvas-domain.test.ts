import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fitCanvas, zoomCanvas } from "@/lib/canvas-geometry";
import type { CanvasDocument, CanvasLayout } from "@/lib/workspace-api";
import type { TeamUser } from "@/lib/team-api";
const user: TeamUser = { username: "synthetic-designer", display_name: "合成设计人员", role: "designer", integration_mode: "managed", auth_context_id: "context-synthetic-1", instance_id: "fixture", scope_id: "fixture-scope" };
const doc: CanvasDocument = { schema_version: "design-canvas/1", project_id: 41, layout_revision: 0, layout: { nodes: [], viewport: { x: 0, y: 0, zoom: 1 } }, updated_at: null, updated_by: null };
const layout: CanvasLayout = { nodes: [{ id: "node_fixture", kind: "version", ref_id: "version-fixture", x: 100, y: 200, width: 480, height: 640 }], viewport: { x: 20, y: 30, zoom: .8 } };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
beforeEach(() => { vi.resetModules(); sessionStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); });
async function setup() { const team = await import("@/lib/team-api"); team.bindDesignContext(user.auth_context_id!, user); return { team, api: await import("@/lib/workspace-api"), engine: await import("@/lib/engine-api") }; }
describe("画布独立修订与原动作恢复（合成HTTP）", () => {
  it("PUT只带布局修订，成功不提升业务revision缓存", async () => {
    const { api, engine } = await setup();
    engine.rememberWorkspace({ project: { id: 41, name: "fixture" }, revision: 9, head: {}, assets: [], specs: [], tasks: [], versions: [] });
    const action = api.freezeLayout(41, doc, layout, user);
    const fetch = vi.fn().mockResolvedValueOnce(response({ ...doc, layout, layout_revision: 1, action_id: action.id, base_layout_revision: 0 })).mockImplementationOnce(async (_url, init) => response({ id: "brief-fixture", action_id: new Headers(init.headers).get("X-Design-Action-Id"), base_revision: 9, revision: 10 })); vi.stubGlobal("fetch", fetch);
    await api.writeLayout(action, user);
    const init = fetch.mock.calls[0][1]; const headers = new Headers(init.headers);
    expect(init.method).toBe("PUT"); expect(headers.has("X-Design-Revision")).toBe(false); expect(JSON.parse(init.body).expected_layout_revision).toBe(0);
    await engine.engineRequest("/project/41/design-briefs", { method: "POST", body: "{}" });
    expect(new Headers(fetch.mock.calls[1][1].headers).get("X-Design-Revision")).toBe("9");
  });
  it("网络unknown冻结原体并阻止新动作，查询layout域才清理", async () => {
    const { api } = await setup(); const action = api.freezeLayout(41, doc, layout, user);
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(response({ action_id: action.id, method: "PUT", path: "/api/project/41/design-canvas", status_code: 200, revision_domain: "layout", result: { ...doc, layout, layout_revision: 1, action_id: action.id, base_layout_revision: 0 } })); vi.stubGlobal("fetch", fetch);
    await expect(api.writeLayout(action, user)).rejects.toThrow();
    expect(api.pendingLayout(41, user)?.body).toBe(action.body); expect(() => api.freezeLayout(41, doc, doc.layout, user)).toThrow();
    await api.queryLayout(action, user); expect(fetch.mock.calls[1][1]?.method ?? "GET").toBe("GET"); expect(api.pendingLayout(41, user)).toBeNull();
  });
  it("换context只读查原布局，不能重发，也不能另账号查", async () => {
    const { api, team } = await setup(); const action = api.freezeLayout(41, doc, layout, user); const next = { ...user, auth_context_id: "context-synthetic-2" }; team.bindDesignContext(next.auth_context_id, next);
    const fetch = vi.fn().mockResolvedValue(response({ action_id: action.id, method: "PUT", path: "/api/project/41/design-canvas", status_code: 200, revision_domain: "layout", result: { ...doc, layout, layout_revision: 1, action_id: action.id, base_layout_revision: 0 } })); vi.stubGlobal("fetch", fetch);
    await expect(api.writeLayout(action, next)).rejects.toThrow("会话已变化"); expect(fetch).not.toHaveBeenCalled();
    await api.queryLayout(action, next); expect(fetch).toHaveBeenCalledTimes(1);
    const other = { ...next, username: "other" }; team.bindDesignContext(other.auth_context_id, other); await expect(api.queryLayout(action, other)).rejects.toThrow("账号已变化");
  });
  it("409布局冲突释放原写，但不把冲突当成功", async () => {
    const { api } = await setup(); const action = api.freezeLayout(41, doc, layout, user); vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ error: { code: "LAYOUT_CONFLICT", message: "布局已有新版本" } }, 409)));
    await expect(api.writeLayout(action, user)).rejects.toThrow("布局已有新版本"); expect(api.pendingLayout(41, user)).toBeNull();
  });
  it("错误业务域回执不能认作布局成功，503未知也保留原体", async () => {
    const { api } = await setup(); const action = api.freezeLayout(41, doc, layout, user);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ error: { code: "HTTP_503", message: "服务未知" } }, 503)));
    await expect(api.writeLayout(action, user)).rejects.toThrow(); expect(api.pendingLayout(41, user)?.id).toBe(action.id);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ action_id: action.id, method: "PUT", path: "/api/project/41/design-canvas", status_code: 200, result: { action_id: action.id, base_revision: 0, revision: 1 } })));
    await expect(api.queryLayout(action, user)).rejects.toThrow("回执尚未确认"); expect(api.pendingLayout(41, user)).toBeTruthy();
  });
  it("单个布局原操作只允许一个在途PUT", async () => {
    const { api } = await setup(); const action = api.freezeLayout(41, doc, layout, user); let finish!: (value: Response) => void;
    const fetch = vi.fn().mockImplementation(() => new Promise<Response>(resolve => { finish = resolve; })); vi.stubGlobal("fetch", fetch);
    const first = api.writeLayout(action, user); await expect(api.writeLayout(action, user)).rejects.toThrow("正在保存"); expect(fetch).toHaveBeenCalledTimes(1);
    finish(response({ ...doc, layout, layout_revision: 1, action_id: action.id, base_layout_revision: 0 })); await first;
  });
});
it("缩放围绕鼠标锚点，适应保留节点比例且限幅", () => {
  const next = zoomCanvas(layout, 2, { x: 200, y: 300 }); expect((200 - next.viewport.x) / next.viewport.zoom).toBeCloseTo((200 - layout.viewport.x) / layout.viewport.zoom);
  const fitted = fitCanvas(layout, 1000, 800); expect(fitted.nodes).toEqual(layout.nodes); expect(fitted.viewport.zoom).toBeCloseTo(680 / 640); expect(zoomCanvas(layout, 10, { x: 0, y: 0 }).viewport.zoom).toBe(4);
});
