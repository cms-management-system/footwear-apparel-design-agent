import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useCanvas } from "@/components/workspace/useCanvas";
import { ApiError, bindDesignContext, type TeamUser } from "@/lib/team-api";
import { canvasStorageKey, freezeLayout, pendingLayout, queryLayout, workspaceApi, writeLayout, type CanvasDocument, type CanvasLayout, type CanvasResult, type LayoutAction } from "@/lib/workspace-api";
vi.mock("@/lib/workspace-api", async original => {
  const real = await original<typeof import("@/lib/workspace-api")>();
  return { ...real, pendingLayout: vi.fn(), queryLayout: vi.fn(), freezeLayout: vi.fn(), writeLayout: vi.fn(), workspaceApi: { ...real.workspaceApi, canvas: vi.fn() } };
});
const user: TeamUser = { username: "owner-a", display_name: "合成设计", role: "designer", integration_mode: "managed", instance_id: "fixture", scope_id: "fixture-scope", auth_context_id: "fixture-context" };
const doc: CanvasDocument = { schema_version: "design-canvas/1", project_id: 71, layout_revision: 0, layout: { nodes: [], viewport: { x: 0, y: 0, zoom: 1 } }, updated_at: null, updated_by: null };
const at = (x: number): CanvasLayout => ({ nodes: [{ id: "node_1", kind: "version", ref_id: "fixture-version", x, y: 0, width: 480, height: 480 }], viewport: { x: 0, y: 0, zoom: 1 } });
const result = (a: LayoutAction): CanvasResult => ({ ...doc, layout_revision: a.base + 1, layout: JSON.parse(a.body).layout, action_id: a.id, base_layout_revision: a.base });
beforeEach(() => {
  vi.useFakeTimers(); vi.clearAllMocks(); sessionStorage.clear(); bindDesignContext(user.auth_context_id!, user);
  vi.mocked(pendingLayout).mockReturnValue(null); vi.mocked(workspaceApi.canvas).mockResolvedValue(doc);
  vi.mocked(freezeLayout).mockImplementation((pid, current, layout) => ({ id: crypto.randomUUID(), projectId: pid, base: current.layout_revision, body: JSON.stringify({ layout }), context: user.auth_context_id!, owner: "fixture" }));
  vi.mocked(writeLayout).mockImplementation(async a => result(a));
});
afterEach(() => { cleanup(); vi.useRealTimers(); bindDesignContext(null); });
async function tick(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
it("仅打开空布局或只读平移不会自动保存", async () => {
  const hook = renderHook(() => useCanvas(71, user, false)); await tick(0); await tick(2000);
  expect(hook.result.current.layout?.nodes).toHaveLength(0); expect(writeLayout).not.toHaveBeenCalled();
  act(() => hook.result.current.change({ ...doc.layout, viewport: { x: 200, y: 20, zoom: 1.2 } })); await tick(2000);
  expect(hook.result.current.dirty).toBe(false); expect(writeLayout).not.toHaveBeenCalled();
});
it("有界防抖合并移动，单在途时后续稿排队且用最新布局修订", async () => {
  let finish!: (value: CanvasResult) => void; vi.mocked(writeLayout).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const hook = renderHook(() => useCanvas(71, user, true)); await tick(0);
  act(() => hook.result.current.change(at(100))); await tick(400); act(() => hook.result.current.change(at(200))); await tick(899); expect(writeLayout).not.toHaveBeenCalled(); await tick(1); expect(writeLayout).toHaveBeenCalledTimes(1);
  act(() => hook.result.current.change(at(300))); await tick(2000); expect(writeLayout).toHaveBeenCalledTimes(1);
  const original = vi.mocked(writeLayout).mock.calls[0][0]; await act(async () => finish(result(original))); await tick(900);
  expect(writeLayout).toHaveBeenCalledTimes(2); const second = vi.mocked(writeLayout).mock.calls[1][0]; expect(second.base).toBe(1); expect(JSON.parse(second.body).layout.nodes[0].x).toBe(300); expect(hook.result.current.state).toBe("saved");
});
it("409保留本地坐标，不静默合并，明确核对后才继续", async () => {
  vi.mocked(writeLayout).mockRejectedValueOnce(new ApiError("LAYOUT_CONFLICT", "冲突", 409)); vi.mocked(workspaceApi.canvas).mockResolvedValueOnce(doc).mockResolvedValue({ ...doc, layout_revision: 2, layout: at(900) });
  const hook = renderHook(() => useCanvas(71, user, true)); await tick(0); act(() => hook.result.current.change(at(300))); await tick(900);
  expect(hook.result.current.state).toBe("conflict"); expect(hook.result.current.layout?.nodes[0].x).toBe(300); expect(hook.result.current.remote?.layout.nodes[0].x).toBe(900); await tick(4000); expect(writeLayout).toHaveBeenCalledTimes(1);
  act(() => hook.result.current.rebase()); await tick(900); expect(vi.mocked(writeLayout).mock.calls[1][0].base).toBe(2); expect(hook.result.current.state).toBe("saved");
});
it("换账号或卸载会阻止旧队列自动写入，草稿按本人隔离", async () => {
  const hook = renderHook(() => useCanvas(71, user, true)); await tick(0); act(() => hook.result.current.change(at(350)));
  const other = { ...user, username: "owner-b" }; bindDesignContext(other.auth_context_id!, other); await tick(900); expect(writeLayout).not.toHaveBeenCalled(); hook.unmount();
  const next = renderHook(() => useCanvas(71, other, true)); await tick(0); expect(next.result.current.layout?.nodes).toHaveLength(0); await tick(3000); expect(writeLayout).not.toHaveBeenCalled();
});
it("服务端生成节点更新只读进入干净画布，刷新不重发生成或布局", async () => {
  const generated = { ...at(900).nodes[0], id: "generated_new", ref_id: "version-new" };
  const fresh = { ...doc, layout_revision: 1, layout: { ...doc.layout, nodes: [generated] } };
  vi.mocked(workspaceApi.canvas).mockResolvedValueOnce(doc).mockResolvedValue(fresh);
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0);
  hook.rerender({ placement: "generated-placed" }); await tick(0); expect(hook.result.current.layout?.nodes).toEqual([generated]); await tick(3000); expect(writeLayout).not.toHaveBeenCalled();
});
it("生成图服务端放置与本地dirty相遇先停队列，明确继续保留新图和本地坐标", async () => {
  const generated = { ...at(900).nodes[0], id: "generated_new", ref_id: "version-new" };
  const fresh = { ...doc, layout_revision: 2, layout: { ...doc.layout, nodes: [generated] } };
  vi.mocked(workspaceApi.canvas).mockResolvedValueOnce(doc).mockResolvedValue(fresh);
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0); act(() => hook.result.current.change(at(300)));
  hook.rerender({ placement: "generated-placed" }); await tick(0); await tick(3000); expect(writeLayout).not.toHaveBeenCalled(); expect(hook.result.current.state).toBe("conflict"); expect(hook.result.current.layout?.nodes[0].x).toBe(300);
  act(() => hook.result.current.rebase()); await tick(900); expect(writeLayout).toHaveBeenCalledTimes(1); const a = vi.mocked(writeLayout).mock.calls[0][0]; expect(a.base).toBe(2); expect(JSON.parse(a.body).layout.nodes.map((n: { ref_id: string }) => n.ref_id)).toEqual(["fixture-version", "version-new"]);
});
it("在途保存的迟到成功不能启动覆盖较新生成节点的后续队列", async () => {
  const generated = { ...at(900).nodes[0], id: "generated_new", ref_id: "version-new" };
  let finish!: (v: CanvasResult) => void; vi.mocked(writeLayout).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  vi.mocked(workspaceApi.canvas).mockResolvedValueOnce(doc).mockResolvedValue({ ...doc, layout_revision: 2, layout: { ...doc.layout, nodes: [generated] } });
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0); act(() => hook.result.current.change(at(300))); await tick(900); act(() => hook.result.current.change(at(400)));
  hook.rerender({ placement: "placed" }); await tick(0); const a = vi.mocked(writeLayout).mock.calls[0][0]; await act(async () => finish(result(a))); await tick(3000);
  expect(writeLayout).toHaveBeenCalledTimes(1); expect(hook.result.current.state).toBe("conflict"); expect(hook.result.current.layout?.nodes[0].x).toBe(400); expect(hook.result.current.remote?.layout.nodes[0].ref_id).toBe("version-new");
});
it("初次rev0延迟晚于placement rev1时不能清掉新图或降低已见布局修订", async () => {
  let initial!: (v: CanvasDocument) => void; const generated = { ...at(900).nodes[0], id: "generated_new", ref_id: "version-new" };
  vi.mocked(workspaceApi.canvas).mockImplementationOnce(() => new Promise(resolve => { initial = resolve; })).mockResolvedValue({ ...doc, layout_revision: 1, layout: { ...doc.layout, nodes: [generated] } });
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0);
  hook.rerender({ placement: "placed" }); await tick(0); expect(hook.result.current.doc?.layout_revision).toBe(1); expect(hook.result.current.layout?.nodes).toEqual([generated]);
  await act(async () => initial(doc)); await tick(3000); expect(hook.result.current.doc?.layout_revision).toBe(1); expect(hook.result.current.layout?.nodes).toEqual([generated]); expect(hook.result.current.state).toBe("clean"); expect(writeLayout).not.toHaveBeenCalled();
});
it("原保存恢复rev1迟到时不能把已读remote rev2降回旧记录", async () => {
  const original: LayoutAction = { id: "fixture-pending-1", projectId: 71, base: 0, body: JSON.stringify({ layout: at(100) }), context: user.auth_context_id!, owner: "fixture" };
  const generated = { ...at(900).nodes[0], id: "generated_new", ref_id: "version-new" };
  vi.mocked(pendingLayout).mockReturnValue(original); vi.mocked(workspaceApi.canvas).mockResolvedValueOnce(doc).mockResolvedValue({ ...doc, layout_revision: 2, layout: { ...doc.layout, nodes: [generated] } });
  let finish!: (v: CanvasResult) => void; vi.mocked(queryLayout).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0);
  let recovery!: Promise<void>; act(() => { recovery = hook.result.current.recover(); }); hook.rerender({ placement: "placed" }); await tick(0); expect(hook.result.current.remote?.layout_revision).toBe(2);
  vi.mocked(pendingLayout).mockReturnValue(null); await act(async () => { finish(result(original)); await recovery; });
  expect(hook.result.current.remote?.layout_revision).toBe(2); expect(hook.result.current.remote?.layout.nodes).toEqual([generated]); expect(hook.result.current.state).toBe("conflict"); expect(hook.result.current.pending).toBeNull(); await tick(3000); expect(writeLayout).not.toHaveBeenCalled();
});
it.each([false, true])("已存草稿先恢复，placement先返后初读迟到也保本地/远端，pending=%s", async hasPending => {
  const draft = { ...at(375), viewport: { x: 51, y: 32, zoom: 1.3 } };
  const draftKey = `${canvasStorageKey(71, user)}:draft`;
  sessionStorage.setItem(draftKey, JSON.stringify({ schema_version: "design-canvas/1", layout: draft }));
  const original: LayoutAction = { id: "fixture-pending-draft", projectId: 71, base: 0, body: JSON.stringify({ layout: at(100) }), context: user.auth_context_id!, owner: "fixture" };
  vi.mocked(pendingLayout).mockReturnValue(hasPending ? original : null);
  let initial!: (v: CanvasDocument) => void; const generated = { ...at(900).nodes[0], id: "generated_new", ref_id: "version-new" };
  vi.mocked(workspaceApi.canvas).mockImplementationOnce(() => new Promise(resolve => { initial = resolve; })).mockResolvedValue({ ...doc, layout_revision: 1, layout: { ...doc.layout, nodes: [generated] } });
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0);
  expect(hook.result.current.layout).toEqual(draft); expect(hook.result.current.dirty).toBe(true);
  hook.rerender({ placement: "placed" }); await tick(0); expect(hook.result.current.layout).toEqual(draft); expect(hook.result.current.remote?.layout.nodes).toEqual([generated]);
  await act(async () => initial(doc)); await tick(3000);
  expect(hook.result.current.layout).toEqual(draft); expect(hook.result.current.remote?.layout_revision).toBe(1); expect(hook.result.current.state).toBe(hasPending ? "unknown" : "conflict"); expect(hook.result.current.pending).toEqual(hasPending ? original : null); expect(writeLayout).not.toHaveBeenCalled(); expect(JSON.parse(sessionStorage.getItem(draftKey)!).layout).toEqual(draft);
});
it("无单独草稿时pending原体先恢复布局，乱序不能让画布一直null", async () => {
  const original: LayoutAction = { id: "fixture-pending-body", projectId: 71, base: 0, body: JSON.stringify({ layout: at(150) }), context: user.auth_context_id!, owner: "fixture" };
  vi.mocked(pendingLayout).mockReturnValue(original); let initial!: (v: CanvasDocument) => void;
  vi.mocked(workspaceApi.canvas).mockImplementationOnce(() => new Promise(resolve => { initial = resolve; })).mockResolvedValue({ ...doc, layout_revision: 1, layout: at(900) });
  const hook = renderHook(({ placement }) => useCanvas(71, user, true, placement), { initialProps: { placement: "[]" } }); await tick(0); expect(hook.result.current.layout).toEqual(at(150));
  hook.rerender({ placement: "placed" }); await tick(0); await act(async () => initial(doc)); expect(hook.result.current.layout).toEqual(at(150)); expect(hook.result.current.remote?.layout_revision).toBe(1); expect(hook.result.current.state).toBe("unknown"); await tick(3000); expect(writeLayout).not.toHaveBeenCalled();
});
