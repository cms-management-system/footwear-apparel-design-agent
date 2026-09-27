import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import DesignChat from "@/components/design/DesignChat";
import SelectedDesignPage from "@/components/design/SelectedDesignPage";
import { agentApi, blankSpec, type StyleDirection, type Workspace } from "@/lib/agent-api";

vi.mock("@/lib/agent-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/agent-api")>();
  return { ...original, agentApi: { ...original.agentApi, capabilities: vi.fn(), projects: vi.fn(), workspace: vi.fn(), create: vi.fn(), upload: vi.fn(), sendMessage: vi.fn(), control: vi.fn(), save: vi.fn(), confirmSpec: vi.fn(), submit: vi.fn(), reviseStylePlan: vi.fn(), confirmStylePlan: vi.fn(), confirmVersion: vi.fn(), saveSamplingSheet: vi.fn(), autoSamplingSheet: vi.fn(), revise: vi.fn(), correctCheck: vi.fn() } };
});
class Stream extends EventTarget {
  static instances: Stream[] = [];
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor() { super(); Stream.instances.push(this); }
  snapshot(value: Workspace) { this.dispatchEvent(new MessageEvent("workspace", { data: JSON.stringify(value) })); }
}
const data = (): Workspace => ({ project: { id: 1, name: "合成对话测试" }, head: { spec_id: "s1" }, messages: [], assets: [], tasks: [], versions: [], specs: [{ id: "s1", status: "draft", fingerprint: "f1", spec: { ...blankSpec(), intent: "方领连衣裙", constraints: [{ id: "c1", kind: "must_keep", text: "保留方领", region: "领口", verification: "visual" }] } }] });
const task = (): Workspace["tasks"][number] => ({ id: "t1", status: "running", outcome: null, questions: [], reasoning_calls: 1, image_calls: 0, reserved_cost_fen: 1, max_cost_fen: 500, steps: [], observations: [] });
const direction = (n: number): StyleDirection => ({ id: `d_${String(n).repeat(12)}`, name: `方向 ${n}`, theme: `主题 ${n}`, rationale: `适合这款连衣裙的理由 ${n}`, explore: `领口与裙摆变化 ${n}`, color_story: `色彩 ${n}`, structure: `结构 ${n}`, material_story: `面料外观 ${n}`, selected: true });
beforeEach(() => {
  vi.resetAllMocks(); Stream.instances = []; localStorage.clear(); sessionStorage.clear();
  vi.stubGlobal("EventSource", Stream);
  vi.stubGlobal("scrollTo", vi.fn());
  vi.stubGlobal("scrollY", 0);
  Object.defineProperty(document.documentElement, "scrollHeight", { value: 0, configurable: true });
  window.history.replaceState(null, "", "/?project=1");
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: false, design: false, vision_service: "未开通", image_service: "未开通", quality_status: "待验", note: "", monthly_allocation_fen: 2000 });
  vi.mocked(agentApi.projects).mockResolvedValue({ items: [{ id: 1, name: "合成对话测试" }] });
  vi.mocked(agentApi.workspace).mockResolvedValue(data());
  vi.mocked(agentApi.sendMessage).mockResolvedValue({ id: "m1", role: "user", text: "裙摆加长", created_at: "test" });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("流式快照即时显示片段，完成消息替换片段，卸载关闭连接", async () => {
  const mounted = render(<DesignChat />);
  await screen.findByLabelText("发消息给设计助手");
  const running = data(); running.tasks = [{ ...task(), live_text: "领口可以" }];
  act(() => Stream.instances[0].snapshot(running));
  expect(screen.getByText("领口可以")).toBeTruthy();
  expect(screen.getByRole("button", { name: "停止本轮" })).toBeTruthy();
  expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
  running.tasks = [{ ...task(), live_text: "领口可以保留方领" }];
  act(() => Stream.instances[0].snapshot(running));
  expect(screen.getByText("领口可以保留方领")).toBeTruthy();
  running.tasks = [{ ...task(), status: "awaiting_review", live_text: "" }];
  running.messages = [{ id: "a1", role: "assistant", text: "领口可以保留方领。", created_at: "test" }];
  act(() => Stream.instances[0].snapshot(running));
  expect(screen.queryByText("正在生成，内容尚未完成")).toBeNull();
  expect(screen.getByText("领口可以保留方领。")).toBeTruthy();
  mounted.unmount(); expect(Stream.instances[0].close).toHaveBeenCalledOnce();
});

it.each(["awaiting_input", "awaiting_review"])("%s 状态直接发送，保持原有额度与追问续接协议", async status => {
  const state = data(); state.tasks = [{ ...task(), status, questions: ["裙长到哪里？"] }];
  state.messages = [{ id: "ask", role: "assistant", text: "裙长到哪里？", created_at: "test" }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: false, vision_service: "测试服务", image_service: "未开通", quality_status: "待验", note: "", monthly_allocation_fen: 2000 });
  render(<DesignChat />);
  fireEvent.change(await screen.findByLabelText("发消息给设计助手"), { target: { value: "到脚踝" } });
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByText(/调整额度|最多 8 次推理/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(agentApi.sendMessage).toHaveBeenCalledWith(1, expect.objectContaining({ text: "到脚踝", expected_spec_id: "s1", authorized: status !== "awaiting_input" })));
});

it("回车发送消息，Shift 回车换行，中文输入法选词不误发送", async () => {
  render(<DesignChat />);
  const input = await screen.findByLabelText("发消息给设计助手");
  fireEvent.change(input, { target: { value: "高跟鞋" } });
  fireEvent.compositionStart(input);
  fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
  expect(agentApi.sendMessage).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input);
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", shiftKey: true });
  expect(agentApi.sendMessage).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
  await waitFor(() => expect(agentApi.sendMessage).toHaveBeenCalledWith(1, expect.objectContaining({ text: "高跟鞋" })));
  expect(agentApi.sendMessage).toHaveBeenCalledTimes(1);
});

it("等待回答时保留结束操作，隐藏内部步骤和预算预留", async () => {
  const state = data();
  state.tasks = [{ ...task(), status: "awaiting_input", questions: ["更偏通勤还是休闲？"], reserved_cost_fen: 100, steps: [{ id: "step-1", tool: "plan", status: "done", summary: "内部步骤" }] }];
  state.messages = [{ id: "ask", role: "assistant", text: "更偏通勤还是休闲？", task_id: "t1", created_at: "test" }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  await screen.findByText("更偏通勤还是休闲？");
  expect(screen.getByRole("button", { name: "结束本轮" })).toBeTruthy();
  expect(screen.queryByText("处理记录")).toBeNull();
  expect(screen.queryByText(/内部步骤|已预留|done/)).toBeNull();
});

it("理解任务等待回答时仍可附加参考图并随回答提交", async () => {
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:test") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  const state = data();
  state.tasks = [{ ...task(), mode: "understand", status: "awaiting_input", questions: ["面料选哪种？"] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: false, vision_service: "测试服务", image_service: "未开通", quality_status: "待验", note: "", monthly_allocation_fen: 2000 });
  vi.mocked(agentApi.upload).mockResolvedValue({ id: "fabric-1", name: "fabric.png", width: 100, height: 100 });
  render(<DesignChat />);
  const imageInput = await screen.findByLabelText(/添加草图、面料照片或参考图/);
  expect((imageInput as HTMLInputElement).disabled).toBe(false);
  fireEvent.change(imageInput, { target: { files: [new File(["image"], "fabric.png", { type: "image/png" })] } });
  fireEvent.change(screen.getByLabelText("发消息给设计助手"), { target: { value: "用这块面料" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(agentApi.sendMessage).toHaveBeenCalledWith(1, expect.objectContaining({
    text: "用这块面料", authorized: true, references: [expect.objectContaining({ asset_id: "fabric-1", role: "structure" })],
  })));
});

it("回执丢失后重试复用消息编号和原始要求版本", async () => {
  vi.mocked(agentApi.sendMessage).mockRejectedValueOnce(new Error("连接超时"));
  render(<DesignChat />);
  fireEvent.change(await screen.findByLabelText("发消息给设计助手"), { target: { value: "裙摆加长" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await screen.findByText("连接超时");
  const newer = data(); newer.head.spec_id = "s2"; newer.specs.push({ ...newer.specs[0], id: "s2" });
  act(() => Stream.instances[0].snapshot(newer));
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(agentApi.sendMessage).toHaveBeenCalledTimes(2));
  expect(vi.mocked(agentApi.sendMessage).mock.calls[1]).toEqual(vi.mocked(agentApi.sendMessage).mock.calls[0]);
});

it("发送已成功但刷新失败，清空已发送内容而不诱导重复提交", async () => {
  render(<DesignChat />);
  fireEvent.change(await screen.findByLabelText("发消息给设计助手"), { target: { value: "裙摆加长" } });
  vi.mocked(agentApi.workspace).mockRejectedValue(new Error("刷新失败"));
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await screen.findByText(/消息已送达/);
  expect((screen.getByLabelText("发消息给设计助手") as HTMLTextAreaElement).value).toBe("");
  expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
  expect(agentApi.sendMessage).toHaveBeenCalledOnce();
});


it("细节在原对话展开，收起保留两个输入，未保存时不发送或确认旧要求", async () => {
  render(<DesignChat />);
  const chat = await screen.findByLabelText("发消息给设计助手");
  fireEvent.change(chat, { target: { value: "尚未发送的聊天" } });
  fireEvent.click(screen.getByRole("button", { name: "查看或调整细节" }));
  const editor = await screen.findByLabelText("想做什么样的设计？");
  expect(screen.getAllByRole("button", { name: "确认这份要求" })).toHaveLength(1);
  expect(screen.getByRole("button", { name: "保存新版本" })).toBeTruthy();
  expect(window.location.pathname + window.location.search).toBe("/?project=1");
  expect(screen.queryByRole("link", { name: "返回设计对话" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "设计助手" })).toBeNull();
  fireEvent.change(editor, { target: { value: "保留方领，只加长裙摆" } });
  fireEvent.click(screen.getByRole("button", { name: "收起，继续聊天" }));
  expect(screen.queryByRole("textbox", { name: "想做什么样的设计？" })).toBeNull();
  expect((screen.getByLabelText("发消息给设计助手") as HTMLTextAreaElement).value).toBe("尚未发送的聊天");
  expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "确认这份要求" }) as HTMLButtonElement).disabled).toBe(true);
  const trigger = screen.getByRole("button", { name: "查看或调整细节" });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "查看或调整细节" }));
  fireEvent.click(trigger);
  expect((screen.getByLabelText("想做什么样的设计？") as HTMLTextAreaElement).value).toBe("保留方领，只加长裙摆");
  expect(agentApi.sendMessage).not.toHaveBeenCalled();
});

it("展开详情跟随实时任务锁，任务完成后可保存且聊天仍在原页面", async () => {
  render(<DesignChat />);
  await screen.findByLabelText("发消息给设计助手");
  fireEvent.click(screen.getByRole("button", { name: "查看或调整细节" }));
  const editor = await screen.findByLabelText("想做什么样的设计？");
  const waiting = data(); waiting.tasks = [{ ...task(), status: "awaiting_input" }];
  act(() => Stream.instances[0].snapshot(waiting));
  expect((editor as HTMLTextAreaElement).disabled).toBe(true);
  const finished = data();
  act(() => Stream.instances[0].snapshot(finished));
  fireEvent.change(editor, { target: { value: "保留方领，只加长裙摆" } });
  const saved = { ...finished.specs[0], id: "s2", spec: { ...finished.specs[0].spec, intent: "保留方领，只加长裙摆" } };
  vi.mocked(agentApi.save).mockResolvedValue(saved);
  vi.mocked(agentApi.workspace).mockResolvedValue({ ...finished, head: { spec_id: "s2" }, specs: [saved] });
  fireEvent.click(screen.getByRole("button", { name: "保存要求单" }));
  await screen.findByText("要求单已保存。请核对后确认。");
  expect(agentApi.save).toHaveBeenCalledWith(1, expect.objectContaining({ intent: "保留方领，只加长裙摆" }), "s1");
  expect(window.location.pathname + window.location.search).toBe("/?project=1");
  fireEvent.click(screen.getByRole("button", { name: "收起，继续聊天" }));
  expect(screen.queryByText(/设计细节有未保存的修改/)).toBeNull();
});


it("只输入品类或仍在追问时不出现要求确认卡，整理完成后才显示", async () => {
  const initial = data(); initial.specs[0].spec.constraints = [];
  vi.mocked(agentApi.workspace).mockResolvedValue(initial);
  render(<DesignChat />);
  await screen.findByLabelText("发消息给设计助手");
  expect(screen.queryByRole("button", { name: "确认这份要求" })).toBeNull();
  const waiting = data(); waiting.tasks = [{ ...task(), status: "awaiting_input" }];
  act(() => Stream.instances[0].snapshot(waiting));
  expect(screen.queryByRole("button", { name: "确认这份要求" })).toBeNull();
  const ready = data(); ready.tasks = [{ ...task(), status: "awaiting_review", proposed_spec_id: "s1" }];
  act(() => Stream.instances[0].snapshot(ready));
  expect(screen.getByRole("heading", { name: "设计要求" })).toBeTruthy();
  expect((screen.getByRole("button", { name: "确认这份要求" }) as HTMLButtonElement).disabled).toBe(false);
});

it("阅读旧消息时流式更新不抢滚动位置，点击最新回复后才跟随", async () => {
  render(<DesignChat />);
  await screen.findByLabelText("发消息给设计助手");
  Object.defineProperty(document.documentElement, "scrollHeight", { value: 2000, configurable: true });
  vi.stubGlobal("scrollY", 120);
  fireEvent.scroll(window);
  vi.mocked(window.scrollTo).mockClear();
  act(() => Stream.instances[0].snapshot({ ...data(), tasks: [{ ...task(), live_text: "正在细化腰线" }] }));
  expect(window.scrollTo).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /查看最新回复/ }));
  expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 2000, behavior: "instant" });
  expect(screen.queryByRole("button", { name: /查看最新回复/ })).toBeNull();
});


it("确认要求后进入风格规划，图片未启用时仍可保存规划", async () => {
  render(<DesignChat />);
  await screen.findByLabelText("发消息给设计助手");
  const confirmed = data(); confirmed.specs[0].status = "confirmed";
  Object.defineProperty(document.documentElement, "scrollHeight", { value: 2000, configurable: true });
  vi.mocked(agentApi.confirmSpec).mockResolvedValue(confirmed.specs[0]);
  vi.mocked(agentApi.workspace).mockResolvedValue(confirmed);
  fireEvent.click(screen.getByRole("button", { name: "确认这份要求" }));
  await screen.findByText("要求已确认并保存。");
  expect(agentApi.confirmSpec).toHaveBeenCalledWith("s1");
  expect(window.scrollTo).toHaveBeenLastCalledWith({ top: 2000, behavior: "instant" });
  expect(screen.getByRole("heading", { name: "先规划几种设计方向？" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "规划 3 个方向" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "生成 3 款" })).toBeNull();
});


it("确认后选择五个方向会持久化选择，并且提交规划不带单次金额上限", async () => {
  const confirmed = data(); confirmed.specs[0].status = "confirmed";
  vi.mocked(agentApi.workspace).mockResolvedValue(confirmed);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: true, vision_service: "test", image_service: "test", quality_status: "test", note: "", monthly_allocation_fen: 2000, image_call_max_fen: 10, reasoning_call_max_fen: 1 });
  vi.mocked(agentApi.submit).mockResolvedValue({ ...task(), status: "queued", mode: "style", design_count: 5 });
  const first = render(<DesignChat />);
  await screen.findByRole("radio", { name: "5 个" });
  expect((screen.getByRole("radio", { name: "3 个" }) as HTMLInputElement).checked).toBe(true);
  fireEvent.click(screen.getByRole("radio", { name: "5 个" }));
  first.unmount();
  render(<DesignChat />);
  await waitFor(() => expect((screen.getByRole("radio", { name: "5 个" }) as HTMLInputElement).checked).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "规划 5 个方向" }));
  await waitFor(() => expect(agentApi.submit).toHaveBeenCalledWith(1, "s1", "style", expect.any(String), 5));
  expect(window.location.pathname + window.location.search).toBe("/?project=1");
});

it("风格规划默认可读，保存选择并确认后才提交对应款数", async () => {
  const state = data(); state.specs[0].status = "confirmed";
  state.head.style_plan_id = "p1";
  state.style_plans = [{ id: "p1", spec_id: "s1", source_fingerprint: "f1", category: "apparel", status: "draft", directions: [direction(1), direction(2), direction(3)] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: true, vision_service: "test", image_service: "test", quality_status: "test", note: "", monthly_allocation_fen: 2000, image_call_max_fen: 10, reasoning_call_max_fen: 1 });
  vi.mocked(agentApi.reviseStylePlan).mockResolvedValue({ ...state.style_plans[0], id: "p2", directions: [direction(1), direction(2), { ...direction(3), selected: false }] });
  vi.mocked(agentApi.confirmStylePlan).mockResolvedValue({ ...state.style_plans[0], status: "confirmed" });
  vi.mocked(agentApi.submit).mockResolvedValue({ ...task(), status: "queued", mode: "design", design_count: 2 });
  render(<DesignChat />);
  await screen.findByRole("heading", { name: "先看方向，再决定出图" });
  expect(screen.getAllByText("调整这条方向").every(item => !item.closest("details")?.open)).toBe(true);
  fireEvent.click(screen.getAllByRole("checkbox", { name: "保留这个方向" })[2]);
  expect((screen.getByRole("button", { name: "确认这 2 个方向" }) as HTMLButtonElement).disabled).toBe(true);
  const revised = { ...state, head: { ...state.head, style_plan_id: "p2" }, style_plans: [{ ...state.style_plans[0], id: "p2", directions: [direction(1), direction(2), { ...direction(3), selected: false }] }] };
  vi.mocked(agentApi.workspace).mockResolvedValue(revised);
  fireEvent.click(screen.getByRole("button", { name: "保存方向调整" }));
  await waitFor(() => expect(agentApi.reviseStylePlan).toHaveBeenCalledWith("p1", expect.arrayContaining([expect.objectContaining({ id: direction(3).id, selected: false })])));
  await screen.findByRole("button", { name: "确认这 2 个方向" });
  const confirmed = { ...revised, style_plans: [{ ...revised.style_plans[0], status: "confirmed" as const }] };
  vi.mocked(agentApi.workspace).mockResolvedValue(confirmed);
  fireEvent.click(screen.getByRole("button", { name: "确认这 2 个方向" }));
  await screen.findByRole("button", { name: "生成 2 款" });
  fireEvent.click(screen.getByRole("button", { name: "生成 2 款" }));
  await waitFor(() => expect(agentApi.submit).toHaveBeenCalledWith(1, "s1", "design", expect.any(String), 2, { id: "p2", directionIds: [direction(1).id, direction(2).id] }));
});


it("需求编辑在设计笔记内展开，不显示重复的调整入口", async () => {
  render(<DesignChat />);
  const toggle = await screen.findByRole("button", { name: "查看或调整细节" });
  vi.mocked(window.scrollTo).mockClear();
  fireEvent.click(toggle);
  const editor = await screen.findByRole("region", { name: "设计细节" });
  expect(window.scrollTo).not.toHaveBeenCalled();
  const transcript = screen.getByRole("region", { name: "设计对话" });
  expect(transcript.contains(editor)).toBe(true);
  expect(editor.parentElement?.textContent).toContain("设计要求");
  expect(screen.queryByRole("button", { name: "查看或调整需求" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "设计版本与逐项检查" })).toBeNull();
});

it("从设计笔记收起需求时保留当前位置和按钮焦点", async () => {
  render(<DesignChat />);
  const toggle = await screen.findByRole("button", { name: "查看或调整细节" });
  fireEvent.click(toggle);
  const localToggle = screen.getByRole("button", { name: "收起，继续聊天" });
  localToggle.focus();
  vi.mocked(window.scrollTo).mockClear();
  fireEvent.click(localToggle);
  expect(screen.queryByRole("region", { name: "设计细节" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "查看或调整细节" }));
  expect(window.scrollTo).not.toHaveBeenCalled();
});

it("方向已规划后收起重复的需求按钮，仍可从顶部修改原始要求", async () => {
  const state = data(); state.specs[0].status = "confirmed";
  state.head.style_plan_id = "p1";
  state.style_plans = [{ id: "p1", spec_id: "s1", source_fingerprint: "f1", category: "apparel", status: "draft", directions: [direction(1), direction(2), direction(3)] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  await screen.findByRole("heading", { name: "先看方向，再决定出图" });
  expect(screen.getByRole("heading", { name: "设计要求" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "查看或调整需求" })).toBeNull();
  expect(screen.queryByRole("button", { name: "要求已确认" })).toBeNull();
  const toggle = screen.getByRole("button", { name: "修改原始需求" });
  fireEvent.click(toggle);
  expect(await screen.findByRole("region", { name: "设计细节" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "收起，继续聊天" }));
  expect(document.activeElement).toBe(toggle);
});

it("两款只完成一款时在方案旁显示中断和继续入口，后续聊天不掩盖进度", async () => {
  const state = data();
  state.tasks = [
    { ...task(), id: "design-task", mode: "design", status: "interrupted", design_count: 2, direction_version_ids: ["v1"], steps: [{ id: "step-2", tool: "generate_design", status: "unknown" }] },
    { ...task(), id: "later-chat", mode: "understand", status: "awaiting_review" },
  ];
  state.versions = [{ id: "v1", task_id: "design-task", spec_id: "s1", status: "ready_for_review", parent_version_id: null, design_index: 1, design_count: 2, review: null }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.control).mockResolvedValue({ ...state.tasks[0], status: "queued" });
  render(<DesignChat />);
  const batch = await screen.findByRole("region", { name: "本次方案" });
  expect(within(batch).getByText(/已生成 1 \/ 2 款/)).toBeTruthy();
  expect(within(batch).getByText(/剩余 1 款出图时连接中断/)).toBeTruthy();
  expect(screen.queryByText(/先比较这 1 款/)).toBeNull();
  fireEvent.click(within(batch).getByRole("button", { name: "继续生成剩余 1 款" }));
  await waitFor(() => expect(agentApi.control).toHaveBeenCalledWith("design-task", "resume"));
});

it("每款方案全宽显示操作并绑定各自版本，切换不丢修改，保存不自动出图", async () => {
  const state = data(); state.specs[0].status = "confirmed";
  state.versions = [1, 2].map(n => ({ id: `v${n}`, spec_id: "s1", status: "ready_for_review", parent_version_id: null, design_index: n, design_count: 2, review: { summary: "已检查", checks: [{ constraint_id: "c1", status: "pass" as const, candidate_region: "领口", evidence: `方案 ${n} 保留了方领` }], goal: { status: "pass", evidence: "符合" }, preservation: { status: "unknown", evidence: "无底图" } } }));
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.revise).mockResolvedValue(state.specs[0]);
  render(<DesignChat />);
  await screen.findByRole("button", { name: "查看方案 2 / 2" });
  fireEvent.click(screen.getByRole("button", { name: "查看方案 2 / 2" }));
  await screen.findByRole("heading", { name: "方案 2 / 2" });
  const second = screen.getByRole("heading", { name: "方案 2 / 2" }).closest("article")!;
  expect(within(second).getByText("方案 2 保留了方领")).toBeTruthy();
  const controls = within(second).getByRole("region", { name: "方案 2 / 2的操作" });
  expect(within(controls).getByRole("heading", { name: "确认或修改此方案" })).toBeTruthy();
  expect(second.querySelector('[class*="selectedVersionLayout"]')?.contains(controls)).toBe(false);
  expect(screen.queryByLabelText("想做什么样的设计？")).toBeNull();
  fireEvent.click(within(controls).getByRole("button", { name: "确认这款" }));
  await waitFor(() => expect(agentApi.confirmVersion).toHaveBeenCalledWith("v2"));
  fireEvent.click(within(controls).getByRole("button", { name: "修改这款" }));
  await waitFor(() => expect((within(controls).getByLabelText("只修改哪个部位？") as HTMLInputElement).disabled).toBe(false));
  fireEvent.change(within(controls).getByLabelText("只修改哪个部位？"), { target: { value: "鞋带" } });
  fireEvent.change(within(controls).getByLabelText("希望怎么修改？"), { target: { value: "把第二款鞋带变细，其余不变" } });
  fireEvent.click(screen.getByRole("button", { name: "查看方案 1 / 2" }));
  expect(screen.getByRole("button", { name: "查看方案 2 / 2" }).textContent).toContain("有未保存修改");
  fireEvent.click(screen.getByRole("button", { name: "查看方案 2 / 2" }));
  expect((within(controls).getByLabelText("希望怎么修改？") as HTMLTextAreaElement).value).toBe("把第二款鞋带变细，其余不变");
  fireEvent.click(within(controls).getByRole("button", { name: "保存修改要求" }));
  await waitFor(() => expect(agentApi.revise).toHaveBeenCalledWith("v2", "把第二款鞋带变细，其余不变", "鞋带", "s1"));
  expect(agentApi.submit).not.toHaveBeenCalled();
  expect(window.location.pathname + window.location.search).toBe("/?project=1");
});

it("已选方案在等待对话回复时可以结束本轮并打开修改弹窗", async () => {
  const state = data();
  state.specs[0].status = "confirmed";
  state.head.confirmed_version_id = "v1";
  state.versions = [{ id: "v1", spec_id: "s1", status: "confirmed", parent_version_id: null, design_index: 1, design_count: 1, review: null }];
  state.tasks = [{ ...task(), status: "awaiting_input", questions: ["还想调整哪里？"] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.control).mockImplementation(async () => {
    const updated = structuredClone(state);
    updated.tasks[0].status = "cancelled";
    vi.mocked(agentApi.workspace).mockResolvedValue(updated);
    return updated.tasks[0];
  });
  render(<DesignChat />);
  const edit = await screen.findByRole("button", { name: "结束当前对话并修改这款" });
  fireEvent.click(edit);
  await waitFor(() => expect(agentApi.control).toHaveBeenCalledWith("t1", "cancel"));
  expect(await screen.findByRole("dialog", { name: "修改方案 1 / 1" })).toBeTruthy();
  expect(screen.getByText("已选定这款")).toBeTruthy();
});

it("选定图片后从独立方案页按需补充打样资料", async () => {
  const state = data();
  state.specs[0].status = "confirmed";
  state.tasks = [{ ...task(), status: "awaiting_input", questions: ["还想调整其他方案吗？"] }];
  state.head.confirmed_version_id = "v1";
  state.versions = [{ id: "v1", spec_id: "s1", status: "confirmed", parent_version_id: null, design_index: 1, design_count: 1, review: { summary: "已检查", checks: [], goal: { status: "pass", evidence: "符合" }, preservation: { status: "pass", evidence: "符合" } } }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.saveSamplingSheet).mockImplementation(async (id, fields) => ({ id: "sheet1", version_id: id, spec_id: "s1", previous_sheet_id: null, status: "draft", fields }));
  render(<SelectedDesignPage versionId="v1" projectId={1} />);
  expect(await screen.findByRole("heading", { name: "方案 1 / 1" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "合成对话测试的方案 1 / 1效果图" })).toBeTruthy();
  expect(screen.queryByLabelText(/面料与辅料/)).toBeNull();
  const panel = screen.getByRole("region", { name: "方案 1 / 1的打样准备" });
  expect(screen.queryByRole("img", { name: "正面结构示意图" })).toBeNull();
  expect(screen.getByRole("region", { name: "技术平面图草稿" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "生成草图" })).toBeTruthy();
  fireEvent.click(within(panel).getByRole("button", { name: "自己补充细节" }));
  fireEvent.change(within(panel).getByLabelText(/面料与辅料/), { target: { value: "全棉面料，克重待核对" } });
  fireEvent.click(within(panel).getByRole("button", { name: "保存打样资料草稿" }));
  await waitFor(() => expect(agentApi.saveSamplingSheet).toHaveBeenCalledWith("v1", expect.objectContaining({ material: "全棉面料，克重待核对" }), null));
  expect(await within(panel).findByText(/打样资料草稿已保存/)).toBeTruthy();
  expect(within(panel).getByRole("link", { name: "下载图片与打样资料" }).getAttribute("href")).toBe("/api/design-versions/v1/delivery");
});

it("独立方案页的一键草稿沿用已确认依据，未知实物参数保持待核对", async () => {
  const state = data();
  state.head.confirmed_version_id = "v1";
  state.versions = [{ id: "v1", spec_id: "s1", status: "confirmed", parent_version_id: null, design_index: 1, design_count: 1, review: null }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.autoSamplingSheet).mockResolvedValue({
    id: "sheet-auto", version_id: "v1", spec_id: "s1", previous_sheet_id: null, status: "draft",
    fields: { material: "", color: "", measurements: "", graphic_placement: "", graphic_dimensions: "", construction: "", notes: "" },
    basis: { intent: "方领连衣裙", requirements: ["保留方领"], visual_review: "视觉检查已通过" },
  });
  render(<SelectedDesignPage versionId="v1" projectId={1} />);
  const panel = await screen.findByRole("region", { name: "方案 1 / 1的打样准备" });
  expect(within(panel).queryByLabelText(/面料与辅料/)).toBeNull();
  fireEvent.click(within(panel).getByRole("button", { name: "自动整理草稿" }));
  await waitFor(() => expect(agentApi.autoSamplingSheet).toHaveBeenCalledWith("v1"));
  expect(await within(panel).findByText(/已整理的设计依据/)).toBeTruthy();
  expect(within(panel).getByText(/待核对：面料与辅料/)).toBeTruthy();
  expect(within(panel).getByRole("link", { name: "下载图片与打样资料" })).toBeTruthy();
});

it("重新打开对话时优先展示已确认款和独立方案页入口", async () => {
  const state = data();
  state.head.confirmed_version_id = "v2";
  state.versions = [1, 2].map(n => ({ id: `v${n}`, spec_id: "s1", status: n === 2 ? "confirmed" : "ready_for_review", parent_version_id: null, design_index: n, design_count: 2, review: null }));
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  await screen.findByRole("button", { name: "查看方案 2 / 2" });
  expect(screen.getByRole("button", { name: "查看方案 2 / 2" }).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByRole("link", { name: "查看这张图的方案页 →" }).getAttribute("href")).toBe("/designs/v2?project=1");
  expect(screen.queryByRole("region", { name: "方案 2 / 2的打样准备" })).toBeNull();
});

it("一款确认后其他款仍可确认，并各自打开方案页", async () => {
  const state = data();
  state.head.confirmed_version_id = "v2";
  state.versions = [1, 2].map(n => ({ id: `v${n}`, spec_id: "s1", status: n === 2 ? "confirmed" : "ready_for_review", parent_version_id: null, design_index: n, design_count: 2, review: { summary: "已检查", checks: [], goal: { status: "pass" as const, evidence: "符合" }, preservation: { status: "pass" as const, evidence: "符合" } } }));
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  fireEvent.click(await screen.findByRole("button", { name: "查看方案 1 / 2" }));
  const first = screen.getByRole("region", { name: "方案 1 / 2的操作" });
  expect((within(first).getByRole("button", { name: "确认这款" }) as HTMLButtonElement).disabled).toBe(false);
  const both = structuredClone(state);
  both.versions[0].status = "confirmed";
  both.head.confirmed_version_id = "v1";
  vi.mocked(agentApi.confirmVersion).mockResolvedValue(both.versions[0]);
  vi.mocked(agentApi.workspace).mockResolvedValue(both);
  fireEvent.click(within(first).getByRole("button", { name: "确认这款" }));
  await waitFor(() => expect(agentApi.confirmVersion).toHaveBeenCalledWith("v1"));
  expect((await within(first).findByRole("link", { name: "查看这张图的方案页 →" })).getAttribute("href")).toBe("/designs/v1?project=1");
  fireEvent.click(screen.getByRole("button", { name: "查看方案 2 / 2" }));
  expect(screen.getByRole("link", { name: "查看这张图的方案页 →" }).getAttribute("href")).toBe("/designs/v2?project=1");
  fireEvent.click(await screen.findByText("已确认 2 款"));
  const confirmedMenu = screen.getByRole("navigation", { name: "已确认款式的方案页" });
  expect(within(confirmedMenu).getByRole("link", { name: "方案 1 / 2" }).getAttribute("href")).toBe("/designs/v1?project=1");
  expect(within(confirmedMenu).getByRole("link", { name: "方案 2 / 2" }).getAttribute("href")).toBe("/designs/v2?project=1");
  cleanup();
  render(<SelectedDesignPage versionId="v2" projectId={1} />);
  expect(await screen.findByRole("heading", { name: "方案 2 / 2" })).toBeTruthy();
  expect(screen.getByRole("region", { name: "首版打样交接" })).toBeTruthy();
});


it("最新三款与旧批次分开，历史图片保留且可展开", async () => {
  const state = data();
  const version = { id: "old", task_id: "old-task", design_index: 1, design_count: 3, status: "candidate", spec_id: "s1", parent_version_id: null, review: null };
  state.versions = [version, ...[1, 2, 3].map(n => ({ ...version, id: `new-${n}`, task_id: "new-task", design_index: n }))];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  const latest = await screen.findByRole("region", { name: "本次方案" });
  expect(within(latest).getByRole("group", { name: "选择要查看的方案" }).querySelectorAll("button")).toHaveLength(3);
  expect(within(latest).getAllByRole("img")).toHaveLength(1);
  expect(within(latest).getAllByRole("heading", { name: "方案 1 / 3" })).toHaveLength(1);
  expect((screen.getByRole("button", { name: "查看方案 1 / 3" }) as HTMLButtonElement).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "查看方案 3 / 3" }));
  expect(screen.getByRole("heading", { name: "方案 3 / 3" })).toBeTruthy();
  expect((screen.getByRole("button", { name: "查看方案 3 / 3" }) as HTMLButtonElement).getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByText("历史方案（1 批）").closest("details")!.open).toBe(false);
  fireEvent.click(screen.getByText("历史方案（1 批）"));
  expect(screen.getByText("历史方案（1 批）").closest("details")!.open).toBe(true);
  const history = screen.getByRole("region", { name: "历史批次 1" });
  expect(within(history).getAllByRole("img")).toHaveLength(1);
  expect(within(history).getByRole("img").getAttribute("src")).toContain("old");
});


it("已保存的单款修改在原方案下恢复、确认并仅提交一张，结果仍留在原方案下", async () => {
  const state = data(); state.specs[0].status = "confirmed";
  state.versions = [1, 2, 3].map(n => ({ id: `v${n}`, task_id: "batch", spec_id: "s1", status: "ready_for_review", parent_version_id: null, design_index: n, design_count: 3, review: null }));
  const revision = { ...state.specs[0], id: "r2", status: "draft", revision_feedback: { text: "第二款鞋带改为黑色", base_spec_id: "s1" }, spec: { ...state.specs[0].spec, base_version_id: "v2", edit_region: "鞋带", constraints: [...state.specs[0].spec.constraints, { id: "edit", kind: "may_change" as const, text: "第二款鞋带改为黑色", region: "鞋带", verification: "visual" as const }] } };
  state.specs.push(revision); state.head.spec_id = "r2";
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: true, vision_service: "test", image_service: "test", quality_status: "test", note: "", monthly_allocation_fen: 5000, image_call_max_fen: 50, reasoning_call_max_fen: 100 });
  render(<DesignChat />);
  const controls = await screen.findByRole("region", { name: "方案 2 / 3的修改要求" });
  expect((screen.getByRole("button", { name: "查看方案 2 / 3" }) as HTMLButtonElement).getAttribute("aria-pressed")).toBe("true");
  const second = screen.getByRole("heading", { name: "方案 2 / 3" }).closest("article")!;
  expect(second.contains(controls)).toBe(true);
  expect(screen.queryByRole("button", { name: "确认这份要求" })).toBeNull();
  expect(screen.queryByRole("group", { name: "生成款数" })).toBeNull();
  expect(screen.getByRole("heading", { name: "设计要求" }).parentElement?.textContent).not.toContain("第二款鞋带改为黑色");
  const confirmed = structuredClone(state); confirmed.specs[1].status = "confirmed";
  vi.mocked(agentApi.confirmSpec).mockResolvedValue(confirmed.specs[1]);
  vi.mocked(agentApi.workspace).mockResolvedValue(confirmed);
  fireEvent.click(within(controls).getByRole("button", { name: "确认修改要求" }));
  const generate = await within(controls).findByRole("button", { name: "生成修改版" });
  await waitFor(() => expect((generate as HTMLButtonElement).disabled).toBe(false));
  expect(within(second).queryByLabelText("只修改哪个部位？")).toBeNull();
  expect(agentApi.confirmSpec).toHaveBeenCalledWith("r2");
  fireEvent.click(generate);
  await waitFor(() => expect(agentApi.submit).toHaveBeenCalledWith(1, "r2", "design", expect.any(String), 1));
  const finished = structuredClone(confirmed);
  finished.versions.push({ id: "child2", task_id: "edit-task", spec_id: "r2", status: "candidate", parent_version_id: "v2", review: null });
  act(() => Stream.instances[0].snapshot(finished));
  expect(within(second).getByRole("heading", { name: "方案 2 / 3 · 修改版 1" })).toBeTruthy();
  const first = document.querySelector<HTMLElement>("#version-v1")!;
  expect(within(first).queryByRole("heading", { name: /修改版/ })).toBeNull();
  expect(within(controls).queryByText("调整这款的完整要求")).toBeNull();
  expect(within(controls).queryByLabelText("想做什么样的设计？")).toBeNull();
  const versionControls = within(second).getByRole("region", { name: "方案 2 / 3的操作" });
  expect(within(versionControls).queryByLabelText("只修改哪个部位？")).toBeNull();
  expect(within(versionControls).queryByLabelText("希望怎么修改？")).toBeNull();
  expect(within(versionControls).queryByRole("button", { name: "生成修改版" })).toBeNull();
  const child = document.querySelector<HTMLElement>("#version-child2")!;
  expect(child.querySelector('[class*="selectedVersionLayout"]')).toBeTruthy();
  expect(child.querySelector('[class*="selectedVersionInfo"]')).toBeTruthy();
  expect(child.querySelector('[class*="selectedVersionLayout"]')?.contains(within(child).getByRole("region", { name: "方案 2 / 3 · 修改版 1的操作" }))).toBe(false);
  expect(within(child).getByRole("button", { name: "修改这款" })).toBeTruthy();
  vi.mocked(agentApi.workspace).mockResolvedValue(finished);
  cleanup();
  render(<DesignChat />);
  const restored = await screen.findByRole("region", { name: "方案 2 / 3的操作" });
  expect(within(restored).queryByLabelText("只修改哪个部位？")).toBeNull();
  expect(screen.getByRole("heading", { name: "方案 2 / 3 · 修改版 1" })).toBeTruthy();
});

it("旧修改版未通过时，新要求仍可从同一底图重新生成", async () => {
  const state = data();
  state.specs[0].status = "confirmed";
  const revision = { ...state.specs[0], id: "red-spec", status: "confirmed", spec: { ...state.specs[0].spec, base_version_id: "white-version", edit_region: "颜色", constraints: [...state.specs[0].spec.constraints, { id: "red", kind: "may_change" as const, text: "换成红色", region: "颜色", verification: "visual" as const }] } };
  state.specs.push(revision);
  state.head.spec_id = "red-spec";
  state.versions = [
    { id: "white-version", spec_id: "s1", status: "ready_for_review", parent_version_id: null, design_index: 1, design_count: 1, review: null },
    { id: "failed-red", spec_id: "old-red-spec", status: "needs_revision", parent_version_id: "white-version", review: null },
  ];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: true, vision_service: "test", image_service: "test", quality_status: "test", note: "", monthly_allocation_fen: 5000, image_call_max_fen: 50, reasoning_call_max_fen: 100 });
  render(<DesignChat />);
  const controls = await screen.findByRole("region", { name: "方案 1 / 1的修改要求" });
  expect(screen.getByRole("button", { name: "查看方案 1 / 1" }).textContent).toContain("有修改要求");
  expect(within(controls).getByText(/换成红色/)).toBeTruthy();
  expect(within(controls).getByRole("button", { name: "生成修改版" })).toBeTruthy();
});

it("出图调用结果未知时阻止直接重试，并允许先结束本轮", async () => {
  const state = data();
  const revision = { ...state.specs[0], id: "black-spec", status: "confirmed", spec: { ...state.specs[0].spec, base_version_id: "second", edit_region: "颜色", constraints: [...state.specs[0].spec.constraints, { id: "black", kind: "may_change" as const, text: "黑色", region: "颜色", verification: "visual" as const }] } };
  state.specs.push(revision);
  state.head.spec_id = "black-spec";
  state.versions = [{ id: "second", spec_id: "s1", status: "ready_for_review", parent_version_id: null, design_index: 2, design_count: 3, review: null }];
  state.tasks = [{ ...task(), status: "interrupted", mode: "design", spec_id: "black-spec", error: { code: "CALL_OUTCOME_UNKNOWN", message: "服务连接中断，本次调用是否计费未知；不会自动重试" }, steps: [{ id: "unknown-step", tool: "edit_design", status: "unknown" }] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: true, vision_service: "test", image_service: "test", quality_status: "test", note: "", monthly_allocation_fen: 5000, image_call_max_fen: 50, reasoning_call_max_fen: 100 });
  render(<DesignChat />);
  const generate = await screen.findByRole("button", { name: "生成修改版" });
  expect((generate as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/没有收到图片/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "恢复本轮" })).toBeNull();
  expect(agentApi.submit).not.toHaveBeenCalled();

  const ended = structuredClone(state);
  ended.tasks[0].status = "cancelled";
  vi.mocked(agentApi.control).mockResolvedValue(ended.tasks[0]);
  vi.mocked(agentApi.workspace).mockResolvedValue(ended);
  fireEvent.click(screen.getByRole("button", { name: "结束本轮" }));
  await waitFor(() => expect(agentApi.control).toHaveBeenCalledWith("t1", "cancel"));
  await waitFor(() => expect((generate as HTMLButtonElement).disabled).toBe(false));
  expect(agentApi.submit).not.toHaveBeenCalled();
});
