import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import CreationHome from "@/components/workspace/CreationHome";
import DesignCanvas, { type CanvasController } from "@/components/workspace/DesignCanvas";
import ProjectConversation from "@/components/workspace/ProjectConversation";
import ExecutionDraft from "@/components/workspace/ExecutionDraft";
import { agentApi, type Workspace } from "@/lib/agent-api";
import { workspaceApi, type Brief, type CanvasLayout, type SelectedContext } from "@/lib/workspace-api";
import { bindDesignContext, type TeamUser } from "@/lib/team-api";
import { readExecutionDraft } from "@/lib/workspace-drafts";
vi.mock("@/lib/agent-api", async importOriginal => { const real = await importOriginal<typeof import("@/lib/agent-api")>(); return { ...real, agentApi: { ...real.agentApi, projects: vi.fn(), workspace: vi.fn(), textToImage: vi.fn(), confirmVersion: vi.fn() } }; });
vi.mock("@/lib/workspace-api", async importOriginal => { const real = await importOriginal<typeof import("@/lib/workspace-api")>(); return { ...real, workspaceApi: { ...real.workspaceApi, message: vi.fn(), brief: vi.fn(), confirm: vi.fn() } }; });
const user: TeamUser = { username: "fixture-owner", display_name: "合成员工", role: "designer", integration_mode: "managed", instance_id: "fixture", scope_id: "fixture-scope", auth_context_id: "fixture-context" };
const fixture = (): Workspace => ({ project: { id: 51, name: "合成画布项目" }, project_context: { project_id: 51, source_mode: "independent", owner_subject: user.username, scope_id: user.scope_id!, revision: 5, status: "working", allowed_actions: ["save_canvas", "send_message", "save_brief", "save_prompt", "generate_image", "select_version"] }, revision: 5, latest_user_message_id: null, execution_brief_state: "ready", current_prompt_id: "prompt-1", head: { spec_id: "spec-1" }, prompts: [{ id: "prompt-1", project_id: 51, positive_prompt: "合成测试：奶白色鞋款", avoid_items: ["不增加标志"], output_kind: "effect_image", design_object: "鞋", base_version_id: null, edit_region: "", source_mode: "independent", derived_from_prompt_id: null, source_brief_id: null, confirmed_through_message_id: null, created_by: user.username, created_at: "", digest: "fixture" }], specs: [{ id: "spec-1", status: "confirmed", fingerprint: "fixture", spec: { intent: "合成测试", references: [], constraints: [], deliverables: "图", base_version_id: null, edit_region: "", conflicts: [], assumptions: [] } }], assets: [], versions: [{ id: "version-1", spec_id: "spec-1", parent_version_id: null, status: "confirmed", review: null }], messages: [], briefs: [], tasks: [], image_only_execution: { available: true, remaining: 1, reason: "ready" } });
const brief = (): Brief => ({ id: "brief-manual-1", project_id: 51, origin: "manual", model_task_id: null, based_on_latest_message_id: null, message_id: null, selected_context: null, based_on_prompt_id: "prompt-1", based_on_spec_id: "spec-1", source_binding_digest: null, positive_prompt: "合成测试：奶白色鞋款", avoid_items: ["不增加标志"], output_kind: "effect_image", base_version_id: null, edit_region: "", created_by: user.username, created_at: "", digest: "fixture", status: "candidate" });
beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); bindDesignContext(user.auth_context_id!, user); vi.mocked(agentApi.projects).mockResolvedValue({ items: [] }); vi.mocked(agentApi.workspace).mockResolvedValue(fixture()); vi.stubGlobal("PointerEvent", MouseEvent); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); bindDesignContext(null); });
it("创作首页使用授权真实项目，快捷用途只预填，原项目独立URL", async () => {
  vi.mocked(agentApi.projects).mockResolvedValue({ items: [{ id: 51, name: "合成画布项目", source_mode: "independent", cover_version_id: "version-1", updated_at: "2026-10-10T08:00:00Z" }] });
  render(<CreationHome user={user} />); const card = await screen.findByRole("link", { name: /合成画布项目/ }); expect(card.getAttribute("href")).toBe("/projects/51");
  fireEvent.click(screen.getByRole("button", { name: "二维设计稿" })); expect((screen.getByLabelText("描述你的鞋服设计想法") as HTMLTextAreaElement).value).toContain("二维设计稿");
  expect(workspaceApi.message).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
function CanvasHarness({ editable = true }: { editable?: boolean }) {
  const [layout, setLayout] = useState<CanvasLayout>({ nodes: [], viewport: { x: 0, y: 0, zoom: 1 } }); const [selected, setSelected] = useState<SelectedContext | null>(null);
  const controller = { layout, change: setLayout, state: "clean", dirty: false, gesture: { current: false }, reread: async () => undefined } as unknown as CanvasController;
  return <><DesignCanvas workspace={fixture()} canvas={controller} editable={editable} selected={selected} onSelect={setSelected} /><output data-testid="layout">{JSON.stringify(layout)}</output><output data-testid="selection">{JSON.stringify(selected)}</output></>;
}
it("空布局不会自行写入，显式加入原图、键盘移动、缩放和选中均零模型", async () => {
  render(<CanvasHarness />); expect(JSON.parse(screen.getByTestId("layout").textContent!).nodes).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "查看项目图片", expanded: false })); fireEvent.click(screen.getByRole("button", { name: "加入画布" }));
  const node = screen.getByRole("button", { name: "画布版本 version-1" }); const old = JSON.parse(screen.getByTestId("layout").textContent!);
  fireEvent.keyDown(node, { key: "ArrowRight" }); expect(JSON.parse(screen.getByTestId("layout").textContent!).nodes[0].x).toBe(old.nodes[0].x + 8);
  fireEvent.click(screen.getByRole("button", { name: "放大画布" })); expect(JSON.parse(screen.getByTestId("layout").textContent!).viewport.zoom).toBeCloseTo(1.2);
  expect(screen.getByTestId("selection").textContent).toContain("version-1"); expect(workspaceApi.message).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it("只读画布可选原图片上下文，不能加入持久节点", () => {
  render(<CanvasHarness editable={false} />); fireEvent.click(screen.getByRole("button", { name: "查看项目图片", expanded: false }));
  expect(screen.queryByRole("button", { name: "加入画布" })).toBeNull(); fireEvent.click(screen.getByRole("button", { name: "设计版本 version-1" }));
  expect(screen.getByTestId("selection").textContent).toContain("version-1"); expect(JSON.parse(screen.getByTestId("layout").textContent!).nodes).toHaveLength(0);
});
it("发送持久消息显式冻结选中图和当前prompt/spec；关闭模型显示系统通知", async () => {
  vi.mocked(workspaceApi.message).mockResolvedValue({ id: "message-1", role: "user", text: "改成浅蓝", created_at: "", reply_state: "unavailable", task_id: null });
  const refresh = vi.fn(async () => undefined);
  render(<ProjectConversation workspace={fixture()} user={user} selected={{ version_id: "version-1", asset_id: null }} onSelect={vi.fn()} editable caps={null} refresh={refresh} onDirty={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("告诉设计助手你的想法"), { target: { value: "改成浅蓝" } }); expect(workspaceApi.message).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "发送项目消息" })); await screen.findByText("消息已保存，当前未启用对话模型。可手动整理执行稿。");
  expect(workspaceApi.message).toHaveBeenCalledWith(51, expect.objectContaining({ expected_prompt_id: "prompt-1", expected_spec_id: "spec-1", selected_context: { version_id: "version-1", asset_id: null }, authorized: false })); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it("发送冲突保留正文，输入法组合Enter不误发", async () => {
  vi.mocked(workspaceApi.message).mockRejectedValue(new Error("PROMPT_CONFLICT"));
  render(<ProjectConversation workspace={fixture()} user={user} selected={null} onSelect={vi.fn()} editable caps={null} refresh={async () => undefined} onDirty={vi.fn()} />);
  const input = screen.getByLabelText("告诉设计助手你的想法"); fireEvent.change(input, { target: { value: "保留方领，改裙摆" } }); fireEvent.keyDown(input, { key: "Enter", ctrlKey: true, isComposing: true }); expect(workspaceApi.message).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "发送项目消息" })); await screen.findByRole("alert"); expect((input as HTMLTextAreaElement).value).toBe("保留方领，改裙摆");
});
it("候选保存不确认或生图，确认按钮使用冻结manual稿", async () => {
  vi.mocked(workspaceApi.brief).mockResolvedValue({ brief: brief(), revision: 6 }); const w = fixture(); const refresh = vi.fn(async () => undefined);
  vi.mocked(workspaceApi.confirm).mockResolvedValue({ prompt: { ...w.prompts![0], id: "prompt-2", source_brief_id: "brief-manual-1" }, spec_id: "spec-2", source_brief_id: "brief-manual-1", revision: 7 });
  render(<ExecutionDraft workspace={w} user={user} selected={null} editable onClose={vi.fn()} onRefresh={refresh} onDirty={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "保存人工候选稿" })); await screen.findByText("人工候选稿已保存，尚未成为执行稿。");
  expect(workspaceApi.confirm).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name: "确认并保存执行稿" })); await screen.findByText(/执行稿已确认保存/);
  expect(workspaceApi.confirm).toHaveBeenCalledWith(51, expect.objectContaining({ candidate_id: "brief-manual-1", positive_prompt: brief().positive_prompt, expected_spec_id: "spec-1" })); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it("新消息needs_confirmation及unknown均阻止旧执行稿生成", () => {
  const w = fixture(); w.latest_user_message_id = "message-new"; w.execution_brief_state = "needs_confirmation";
  const view = render(<ExecutionDraft workspace={w} user={user} selected={null} editable onClose={vi.fn()} onRefresh={async () => undefined} onDirty={vi.fn()} />);
  expect((screen.getByRole("button", { name: "生成一张设计图" }) as HTMLButtonElement).disabled).toBe(true);
  w.execution_brief_state = "ready"; w.latest_user_message_id = null; w.tasks = [{ id: "task-unknown", status: "interrupted", outcome: "unknown", questions: [], steps: [], observations: [], reasoning_calls: 0, image_calls: 1, reserved_cost_fen: 0, max_cost_fen: null }];
  view.rerender(<ExecutionDraft workspace={w} user={user} selected={null} editable onClose={vi.fn()} onRefresh={async () => undefined} onDirty={vi.fn()} />);
  expect((screen.getByRole("button", { name: "生成一张设计图" }) as HTMLButtonElement).disabled).toBe(true); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it("尚未发送及409草稿在失权卸载后按本人显式恢复，账号/项目隔离", async () => {
  vi.mocked(workspaceApi.brief).mockRejectedValue(new Error("BRIEF_CONFLICT"));
  const props = { workspace: fixture(), user, selected: null, editable: true, onClose: vi.fn(), onRefresh: async () => undefined, onDirty: vi.fn() };
  const view = render(<ExecutionDraft {...props} />); const text = "未发送完整执行稿。".repeat(100);
  fireEvent.change(screen.getByLabelText("完整正向设计描述"), { target: { value: text } }); fireEvent.change(screen.getByLabelText("避免项（每行一项）"), { target: { value: "不增加标志\n不要透明面料" } });
  fireEvent.click(screen.getByRole("button", { name: "保存人工候选稿" })); await screen.findByRole("alert"); view.unmount();
  expect(readExecutionDraft(user, 51)?.text).toBe(text); expect(readExecutionDraft({ ...user, username: "other" }, 51)).toBeNull(); expect(readExecutionDraft(user, 52)).toBeNull();
  const restored = render(<ExecutionDraft {...props} user={{ ...user, auth_context_id: "new-context" }} />);
  expect((screen.getByLabelText("完整正向设计描述") as HTMLTextAreaElement).value).not.toBe(text); fireEvent.click(screen.getByRole("button", { name: "恢复本人执行稿草稿" }));
  await waitFor(() => expect((screen.getByLabelText("完整正向设计描述") as HTMLTextAreaElement).value).toBe(text)); expect((screen.getByLabelText("避免项（每行一项）") as HTMLTextAreaElement).value).toContain("不要透明面料"); expect(workspaceApi.brief).toHaveBeenCalledTimes(1); restored.unmount();
});

it("指针拖动和手工具平移采用世界/屏幕坐标，适应不改节点尺寸", () => {
  render(<CanvasHarness />); fireEvent.click(screen.getByRole("button", { name: "查看项目图片", expanded: false })); fireEvent.click(screen.getByRole("button", { name: "加入画布" }));
  const node = screen.getByRole("button", { name: "画布版本 version-1" }); const surface = screen.getByRole("application"); const original = JSON.parse(screen.getByTestId("layout").textContent!);
  fireEvent.pointerDown(node, { button: 0, clientX: 100, clientY: 100 }); fireEvent.pointerMove(surface, { clientX: 160, clientY: 180 }); fireEvent.pointerUp(surface);
  const moved = JSON.parse(screen.getByTestId("layout").textContent!); expect(moved.nodes[0].x).toBe(original.nodes[0].x + 60); expect(moved.nodes[0].y).toBe(original.nodes[0].y + 80);
  fireEvent.click(screen.getByRole("button", { name: "平移画布" })); fireEvent.pointerDown(surface, { button: 0, clientX: 100, clientY: 100 }); fireEvent.pointerMove(surface, { clientX: 150, clientY: 120 }); fireEvent.pointerUp(surface);
  expect(JSON.parse(screen.getByTestId("layout").textContent!).viewport.x).toBe(50); expect(JSON.parse(screen.getByTestId("layout").textContent!).nodes).toEqual(moved.nodes);
  fireEvent.click(screen.getByRole("button", { name: "适应画布" })); expect(JSON.parse(screen.getByTestId("layout").textContent!).nodes).toEqual(moved.nodes); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it("管理者无输入或写稿，候选新消息陈旧与修改区域要求可见", async () => {
  const w = fixture(); const b = brief(); w.briefs = [b]; w.latest_user_message_id = "message-new"; w.execution_brief_state = "needs_confirmation";
  const view = render(<ExecutionDraft workspace={w} user={{ ...user, role: "manager" }} selected={null} editable={false} onClose={vi.fn()} onRefresh={async () => undefined} onDirty={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "保存人工候选稿" })).toBeNull(); expect(screen.queryByRole("button", { name: "生成一张设计图" })).toBeNull(); view.unmount();
  render(<ExecutionDraft workspace={fixture()} user={user} selected={{ version_id: "version-1", asset_id: null }} editable onClose={vi.fn()} onRefresh={async () => undefined} onDirty={vi.fn()} />);
  expect((screen.getByRole("button", { name: "保存人工候选稿" }) as HTMLButtonElement).disabled).toBe(true); fireEvent.change(screen.getByLabelText("修改区域"), { target: { value: "鞋面配色" } });
  await waitFor(() => expect((screen.getByRole("button", { name: "保存人工候选稿" }) as HTMLButtonElement).disabled).toBe(false)); expect((screen.getByRole("button", { name: "生成一张设计图" }) as HTMLButtonElement).disabled).toBe(true);
});

it("选图只设上下文，人工选定原版本仍不调用模型或负责人批准", async () => {
  const w = fixture(); w.versions[0].status = "ready_for_review"; vi.mocked(agentApi.confirmVersion).mockResolvedValue({ ...w.versions[0], status: "confirmed" });
  render(<ProjectConversation workspace={w} user={user} selected={{ version_id: "version-1", asset_id: null }} onSelect={vi.fn()} editable caps={null} refresh={async () => undefined} onDirty={vi.fn()} />);
  expect(agentApi.confirmVersion).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name: "选定此设计方案" })); await screen.findByText(/已人工选定，负责人审查仍需单独完成/);
  expect(agentApi.confirmVersion).toHaveBeenCalledWith("version-1"); expect(agentApi.textToImage).not.toHaveBeenCalled(); expect(workspaceApi.message).not.toHaveBeenCalled();
});

it("候选引用本人最新消息，历史其他作者消息仅可查看不被认领", async () => {
  const w = fixture(); w.latest_user_message_id = "history-user"; w.messages = [{ id: "history-user", created_by: "other-owner", created_at: "", role: "user", text: "历史讨论要求" }]; w.execution_brief_state = "needs_confirmation";
  vi.mocked(workspaceApi.brief).mockResolvedValue({ brief: { ...brief(), based_on_latest_message_id: "history-user" }, revision: 6 });
  render(<ExecutionDraft workspace={w} user={user} selected={null} editable onClose={vi.fn()} onRefresh={async () => undefined} onDirty={vi.fn()} />);
  expect(screen.getByText("历史讨论要求")).toBeTruthy(); fireEvent.click(screen.getByRole("button", { name: "保存人工候选稿" })); await screen.findByText("人工候选稿已保存，尚未成为执行稿。"); expect(workspaceApi.brief).toHaveBeenCalledWith(51, expect.objectContaining({ message_id: null })); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
