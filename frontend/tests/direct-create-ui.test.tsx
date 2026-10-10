import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import CreationHome, { ProjectCard } from "@/components/workspace/CreationHome";
import ProjectMenu from "@/components/workspace/ProjectMenu";
import CreationRuns from "@/components/workspace/CreationRuns";
import ProjectConversation from "@/components/workspace/ProjectConversation";
import { useSendKey } from "@/components/workspace/useSendKey";
import { executeDirect, freezeDirect, loadDirect } from "@/lib/direct-create";
import { executeProjectAction, freezeProjectAction } from "@/lib/project-metadata";
import { agentApi, type Capabilities, type CreationRun, type Workspace } from "@/lib/agent-api";
import { workspaceApi } from "@/lib/workspace-api";
import { bindDesignContext, type TeamUser } from "@/lib/team-api";
vi.mock("@/lib/agent-api", async orig => { const real = await orig<typeof import("@/lib/agent-api")>(); return { ...real, agentApi: { ...real.agentApi, projects: vi.fn(), workspace: vi.fn(), textToImage: vi.fn() } }; });
vi.mock("@/lib/direct-create", async orig => { const real = await orig<typeof import("@/lib/direct-create")>(); return { ...real, freezeDirect: vi.fn(), executeDirect: vi.fn(), loadDirect: vi.fn(() => null) }; });
vi.mock("@/lib/project-metadata", async orig => { const real = await orig<typeof import("@/lib/project-metadata")>(); return { ...real, executeProjectAction: vi.fn(), freezeProjectAction: vi.fn() }; });
vi.mock("@/lib/workspace-api", async orig => { const real = await orig<typeof import("@/lib/workspace-api")>(); return { ...real, workspaceApi: { ...real.workspaceApi, message: vi.fn() } }; });
const user: TeamUser = { username: "ui-owner", display_name: "合成", role: "designer", integration_mode: "managed", instance_id: "fixture", scope_id: "scope", auth_context_id: "ctx" };
const action = { id: "fixture-direct-0001", actor: user.username, scope: user.scope_id!, instance: user.instance_id!, path: "/design-projects/direct", method: "POST", context: "ctx", body: "{}", created_at: "" };
beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); bindDesignContext("ctx", user); vi.mocked(agentApi.projects).mockResolvedValue({ items: [] }); vi.mocked(loadDirect).mockReturnValue(null); vi.mocked(freezeDirect).mockReturnValue(action); vi.mocked(executeDirect).mockImplementation(() => new Promise(() => {})); });
afterEach(() => { cleanup(); bindDesignContext(null); });
function Keys({ send }: { send: () => void }) { const keys = useSendKey(send); return <textarea aria-label="合成键盘" {...keys} />; }
it("Enter一次提交，Shift换行、repeat与IME生命周期/229均不误发", () => {
  const send = vi.fn(); render(<Keys send={send} />); const box = screen.getByLabelText("合成键盘");
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true }); fireEvent.keyDown(box, { key: "Enter", keyCode: 229 }); fireEvent.compositionStart(box); fireEvent.keyDown(box, { key: "Enter" }); fireEvent.compositionEnd(box); expect(send).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Enter" }); fireEvent.keyDown(box, { key: "Enter", repeat: true }); fireEvent.keyDown(box, { key: "Enter" }); expect(send).toHaveBeenCalledTimes(1);
  fireEvent.keyUp(box, { key: "Enter" }); fireEvent.keyDown(box, { key: "Enter" }); expect(send).toHaveBeenCalledTimes(2);
});
it("首页4000统一、原文一次直接创建，双Enter/发送不另发首消息或图", async () => {
  render(<CreationHome user={user} />); const box = screen.getByLabelText("描述你的鞋服设计想法"); expect(box.getAttribute("maxlength")).toBe("4000");
  fireEvent.change(box, { target: { value: " 帮我设计秋季通勤外套。\n保留圆领 " } }); fireEvent.keyDown(box, { key: "Enter" }); fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(executeDirect).toHaveBeenCalledTimes(1)); expect(freezeDirect).toHaveBeenCalledWith(user, { entry_mode: "idea", text: " 帮我设计秋季通勤外套。\n保留圆领 ", output_kind: "effect_image", intent: "generate_image", authorized: true }); expect(workspaceApi.message).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled(); expect(screen.queryByLabelText("设计名称")).toBeNull();
});
it("空白新建显式一次无授权，无mount自动创建", async () => {
  render(<CreationHome user={user} />); await screen.findByRole("button", { name: /新建项目/ }); expect(executeDirect).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole("button", { name: /新建项目/ }));
  expect(freezeDirect).toHaveBeenCalledWith(user, { entry_mode: "blank", text: "", output_kind: "effect_image", intent: "none", authorized: false });
});
it("菜单与Link是兄弟，键盘定位/Escape归焦，取消删除零写", async () => {
  render(<ProjectCard project={{ id: 91, name: "合成鞋", revision: 6, cover_version_id: null, allowed_project_actions: ["rename", "archive"] }} user={user} onChanged={vi.fn()} />);
  const trigger = screen.getByRole("button", { name: "合成鞋的项目菜单" }); expect(trigger.closest("a")).toBeNull(); fireEvent.click(trigger);
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "重命名" })));
  fireEvent.keyDown(screen.getByRole("menu"), { key: "End" }); expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "删除项目" })); fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" }); expect(document.activeElement).toBe(trigger);
  fireEvent.click(trigger); fireEvent.click(screen.getByRole("menuitem", { name: "删除项目" })); expect(screen.getByText(/项目历史和图片保留/)).toBeTruthy(); fireEvent.click(screen.getByRole("button", { name: "取消" })); expect(executeProjectAction).not.toHaveBeenCalled();
});
it("重命名只在真实成功后刷新，失败保输入；无权菜单不可写", async () => {
  const onChanged = vi.fn(); vi.mocked(freezeProjectAction).mockReturnValue({ ...action, projectId: 91, revision: 6 }); vi.mocked(executeProjectAction).mockRejectedValue(new Error("修订冲突"));
  const view = render(<ProjectMenu project={{ id: 91, name: "旧标题", revision: 6, allowed_project_actions: ["rename"] }} user={user} onChanged={onChanged} />);
  fireEvent.click(screen.getByRole("button", { name: "旧标题的项目菜单" })); fireEvent.click(screen.getByRole("menuitem", { name: "重命名" })); fireEvent.change(screen.getByLabelText("项目名称"), { target: { value: "新标题" } }); fireEvent.click(screen.getByRole("button", { name: "保存名称" })); await screen.findByText("修订冲突"); expect(onChanged).not.toHaveBeenCalled(); expect((screen.getByLabelText("项目名称") as HTMLInputElement).value).toBe("新标题"); expect(freezeProjectAction).toHaveBeenCalledWith(91, 6, user, "新标题");
  view.unmount(); render(<ProjectMenu project={{ id: 92, name: "上游只读", allowed_project_actions: [] }} user={user} onChanged={onChanged} />); fireEvent.click(screen.getByRole("button", { name: "上游只读的项目菜单" })); expect((screen.getByRole("menuitem", { name: "删除项目" }) as HTMLButtonElement).disabled).toBe(true);
});
const run = (): CreationRun => ({ id: "r1", project_id: 91, intent_id: "i", message_id: "m", status: "succeeded", stage: "done", reason: null, error: null, text_task_id: "text1", image_task_id: "image1", prompt_id: "p", spec_id: "s", version_id: "v1", base_version_id: null, created_at: "", updated_at: "", steps: [], canvas_placement: "placed" });
it("刷新/重挂只呈现真实run图，不发模型；未知结果显示核对，放置失败不假丢图", () => {
  const refresh = vi.fn(async () => undefined); const onSelect = vi.fn(); const props = { projectId: 91, onSelect, refresh, onError: vi.fn() };
  const view = render(<CreationRuns runs={[run()]} {...props} />); expect(screen.getByAltText("本次任务实际生成的设计图").getAttribute("src")).toContain("v1"); fireEvent.click(screen.getByRole("button", { name: "查看本次生成图片" })); expect(onSelect).toHaveBeenCalledWith({ version_id: "v1", asset_id: null });
  view.rerender(<CreationRuns runs={[{ ...run(), canvas_placement: "conflict" }]} {...props} />); expect(screen.getByText(/图片已生成，待加入画布/)).toBeTruthy(); expect(executeDirect).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled();
  view.rerender(<CreationRuns runs={[{ ...run(), version_id: null, status: "unknown", reason: "供应商请求待核对" }]} {...props} />); expect(screen.getByText("供应商请求待核对")).toBeTruthy(); expect(screen.queryByRole("img")).toBeNull();
});
it("项目Enter auto意图一次并保原文；文本可用/生成不可用仍可讨论，未造assistant", async () => {
  const w: Workspace = { project: { id: 91, name: "测试" }, project_context: { project_id: 91, source_mode: "independent", owner_subject: user.username, scope_id: "scope", revision: 1, status: "draft", allowed_actions: ["send_message"] }, head: {}, specs: [], assets: [], tasks: [], versions: [], messages: [] };
  vi.mocked(workspaceApi.message).mockResolvedValue({ id: "m1", role: "user", text: "谈谈鞋底", created_at: "", run_id: "r2" });
  render(<ProjectConversation workspace={w} user={user} editable selected={null} onSelect={vi.fn()} refresh={async () => undefined} onDirty={vi.fn()} caps={{ understand: true, design: false, vision_service: "off", image_service: "off", quality_status: "", note: "", monthly_allocation_fen: 0, direct_creation: { available: false, reason: "无图片额度", text: { available: true, request_model: "Gemini", response_model: null }, image: { available: false, request_model: null, response_model: null, single_image: true }, remaining: { text_calls: 1, image_calls: 0 } } }} />);
  const box = screen.getByLabelText("告诉设计助手你的想法"); fireEvent.change(box, { target: { value: " 谈谈鞋底\n " } }); fireEvent.keyDown(box, { key: "Enter" }); await screen.findByText("想法已保存，创作进度将在这里更新。"); expect(workspaceApi.message).toHaveBeenCalledWith(91, expect.objectContaining({ text: " 谈谈鞋底\n ", authorized: true, intent: "auto", output_kind: null })); expect(screen.getByText("当前可讨论设计；图片生成暂不可用")).toBeTruthy(); expect(screen.queryByText("设计助手")).toBeNull(); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
const exhausted = (reason = "CALL_LIMIT_EXHAUSTED"): Capabilities => ({ understand: true, design: true, vision_service: "off", image_service: "image", quality_status: "", note: "", monthly_allocation_fen: 0, direct_creation: { available: false, reason, text: { available: false, request_model: "Gemini", response_model: null }, image: { available: false, request_model: "Image2", response_model: null, single_image: true }, remaining: { text_calls: 0, image_calls: 0 } } });
const progressWorkspace = (runs: CreationRun[] = []): Workspace => ({ project: { id: 91, name: "进度展示合成" }, head: {}, specs: [], assets: [], tasks: [], versions: [], messages: [], creation_runs: runs });
it("已预留的queued/text/image创作优先展示真实进度，余额不足不冒充当前任务失败", () => {
  const props = { user, selected: null, onSelect: vi.fn(), editable: true, caps: exhausted(), refresh: vi.fn(async () => undefined), onDirty: vi.fn() };
  const view = render(<ProjectConversation {...props} workspace={progressWorkspace([{ ...run(), status: "queued", stage: "text", version_id: null }])} />);
  expect(screen.getByText("本次创作已排队，请稍候。")).toBeTruthy();
  view.rerender(<ProjectConversation {...props} workspace={progressWorkspace([{ ...run(), status: "running", stage: "text", version_id: null }])} />); expect(screen.getByText("正在理解你的设计想法…")).toBeTruthy();
  view.rerender(<ProjectConversation {...props} workspace={progressWorkspace([{ ...run(), status: "running", stage: "image", version_id: null }])} />); expect(screen.getByText("正在生成设计图片…")).toBeTruthy();
  expect(screen.queryByText("CALL_LIMIT_EXHAUSTED")).toBeNull(); expect(screen.queryByText(/当前创作额度已预留或用完/)).toBeNull(); expect((screen.getByRole("button", { name: "发送项目消息" }) as HTMLButtonElement).disabled).toBe(true);
  expect(workspaceApi.message).not.toHaveBeenCalled(); expect(executeDirect).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it.each([
  ["CALL_LIMIT_EXHAUSTED", "当前创作额度已预留或用完，暂时不能开始新的创作。"],
  ["BUDGET_EXHAUSTED", "本次创作预算已用完，暂时不能开始新的创作。"],
  ["CREATION_AUTHORIZATION_REQUIRED", "当前项目尚未获得创作授权，消息仍可保存。"],
  ["UNKNOWN_INTERNAL_REASON", "暂时无法开始新的创作，已有内容仍保留。"],
])("无活动任务的不可用原因%s显示中文，不透出内部码", (reason, message) => {
  render(<ProjectConversation workspace={progressWorkspace()} user={user} selected={null} onSelect={vi.fn()} editable caps={exhausted(reason)} refresh={async () => undefined} onDirty={vi.fn()} />);
  expect(screen.getByText(message)).toBeTruthy(); expect(screen.queryByText(reason)).toBeNull(); expect(workspaceApi.message).not.toHaveBeenCalled(); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
it("真实blocked记录的reason码也用中文解释，保留实际错误和状态", () => {
  render(<CreationRuns runs={[{ ...run(), status: "blocked", version_id: null, reason: "CALL_LIMIT_EXHAUSTED", error: { code: "CALL_LIMIT_EXHAUSTED", message: "本次授权的调用次数已预留或用完" } }]} projectId={91} onSelect={vi.fn()} refresh={async () => undefined} onError={vi.fn()} />);
  expect(screen.getByText("当前无法开始")).toBeTruthy(); expect(screen.getByText("当前创作额度已预留或用完，暂时不能开始新的创作。")).toBeTruthy(); expect(screen.getByText("本次授权的调用次数已预留或用完")).toBeTruthy(); expect(screen.queryByText("CALL_LIMIT_EXHAUSTED")).toBeNull(); expect(agentApi.textToImage).not.toHaveBeenCalled();
});
