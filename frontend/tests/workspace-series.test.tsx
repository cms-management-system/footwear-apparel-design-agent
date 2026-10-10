import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import DesignChat from "@/components/design/DesignChat";
import SelectedDesignPage from "@/components/design/SelectedDesignPage";
import Model3DPanel from "@/components/design/Model3DPanel";
import TechnicalFlatPanel from "@/components/design/TechnicalFlatPanel";
import { agentApi, blankSpec, type Capabilities, type Version, type Workspace } from "@/lib/agent-api";
import { bindDesignContext } from "@/lib/team-api";

vi.mock("@/components/team/ProjectSource", () => ({ default: ({ projectId }: { projectId: number }) => <p data-testid="project-source-probe">来源项目 {projectId}</p> }));

vi.mock("@/lib/agent-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/agent-api")>();
  return { ...original, agentApi: { ...original.agentApi, capabilities: vi.fn(), projects: vi.fn(), workspace: vi.fn(), create: vi.fn(), upload: vi.fn(), sendMessage: vi.fn(), control: vi.fn(), save: vi.fn(), confirmSpec: vi.fn(), submit: vi.fn(), reviseStylePlan: vi.fn(), confirmStylePlan: vi.fn(), confirmVersion: vi.fn(), recheckVersion: vi.fn(), saveSamplingSheet: vi.fn(), autoSamplingSheet: vi.fn(), revise: vi.fn(), correctCheck: vi.fn(), generate3D: vi.fn(), generateTechnicalFlat: vi.fn(), reviseTechnicalFlat: vi.fn(), confirmTechnicalFlat: vi.fn() } };
});
class Stream extends EventTarget {
  static instances: Stream[] = [];
  onerror: (() => void) | null = null;
  close = vi.fn();
  constructor() { super(); Stream.instances.push(this); }
  snapshot(value: Workspace) { this.dispatchEvent(new MessageEvent("workspace", { data: JSON.stringify(value) })); }
}
const capabilities: Capabilities = { understand: false, design: false, vision_service: "未启用", image_service: "未启用", quality_status: "未验", note: "合成测试", monthly_allocation_fen: 0, three_d: { enabled: false, provider: "未启用", estimated_cost_fen: 0, monthly_allocation_fen: 0, note: "未启用" } };
const candidate = (id = "v1"): Version => ({ id, spec_id: "s1", status: "ready_for_review", parent_version_id: null, design_index: 1, design_count: 1, review: { summary: "合成检查", checks: [], goal: { status: "pass", evidence: "合成证据" }, preservation: { status: "unknown", evidence: "待核对" } } });
const workspace = (id = 1): Workspace => ({ project: { id, name: `合成任务 ${id}` }, head: { spec_id: "s1" }, assets: [], tasks: [], versions: [], messages: [], specs: [{ id: "s1", status: "draft", fingerprint: "f1", spec: { ...blankSpec(), intent: `任务 ${id} 的连衣裙`, constraints: [{ id: "c1", kind: "must_keep", text: "保留腰线", region: "腰部", verification: "visual" }] } }] });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
beforeEach(() => {
  vi.resetAllMocks(); Stream.instances = []; sessionStorage.clear(); localStorage.clear(); bindDesignContext("test-context");
  vi.stubGlobal("EventSource", Stream); vi.stubGlobal("scrollTo", vi.fn()); vi.stubGlobal("confirm", vi.fn(() => true));
  window.history.replaceState(null, "", "/?project=1");
  vi.mocked(agentApi.capabilities).mockResolvedValue(capabilities);
  vi.mocked(agentApi.projects).mockResolvedValue({ items: [{ id: 1, name: "合成任务 1" }, { id: 2, name: "合成任务 2" }] });
  vi.mocked(agentApi.workspace).mockImplementation(async id => workspace(id));
  vi.mocked(agentApi.sendMessage).mockResolvedValue({ id: "message", role: "user", text: "合成说明", created_at: "test" });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); bindDesignContext(null); });

it("SSE对象失权立即移除原私有内容并停止连接", async () => {
  render(<DesignChat />);
  await screen.findByRole("heading", { name: "合成任务 1" });
  expect(screen.queryAllByText("任务 1 的连衣裙").length).toBeGreaterThan(0);
  const source = Stream.instances.at(-1)!;
  act(() => source.dispatchEvent(new MessageEvent("error", { data: JSON.stringify({ error: { code: "NOT_FOUND" } }) })));
  expect(screen.queryAllByText("任务 1 的连衣裙")).toHaveLength(0);
  expect(screen.getByRole("alert").textContent).toContain("访问权限已变化");
  expect(source.close).toHaveBeenCalled();
});

it("无项目入口展示授权导航，不自动新建或导入CMS", async () => {
  window.history.replaceState(null, "", "/");
  render(<DesignChat />);
  expect(await screen.findByRole("heading", { name: "查看团队的设计项目" })).toBeTruthy();
  expect(screen.getByRole("link", { name: "查看上游任务 →" }).getAttribute("href")).toBe("/handoffs");
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByText(/从 CMS 导入/)).toBeNull();
  expect(agentApi.create).not.toHaveBeenCalled();
  expect(agentApi.workspace).not.toHaveBeenCalled();
});

it("管理者只读方案，不呈现消息上传并禁止员工确认和修改", async () => {
  const state = workspace(); state.versions = [candidate()];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat readOnly />);
  await screen.findByRole("heading", { name: "合成任务 1" });
  expect(screen.getByText(/当前设计为只读查看/)).toBeTruthy();
  expect(screen.queryByLabelText("发消息给设计助手")).toBeNull();
  expect(screen.queryByLabelText(/添加草图/)).toBeNull();
  for (const name of ["确认这份要求", "确认这款", "修改这款"]) {
    const button = screen.getByRole("button", { name }) as HTMLButtonElement;
    expect(button.disabled).toBe(true); fireEvent.click(button);
  }
  expect(agentApi.confirmSpec).not.toHaveBeenCalled();
  expect(agentApi.confirmVersion).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("项目切换后旧HTTP与SSE快照不能覆盖当前任务", async () => {
  const old = deferred<Workspace>();
  vi.mocked(agentApi.workspace).mockImplementation(id => id === 1 ? old.promise : Promise.resolve(workspace(id)));
  render(<DesignChat />);
  fireEvent.change(await screen.findByLabelText("选择设计对话"), { target: { value: "2" } });
  await screen.findByRole("heading", { name: "合成任务 2" });
  await act(async () => old.resolve(workspace(1)));
  act(() => Stream.instances[0].snapshot(workspace(1)));
  expect(screen.getByRole("heading", { name: "合成任务 2" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "合成任务 1" })).toBeNull();
  expect(Stream.instances[0].close).toHaveBeenCalled();
});

it("已收到较新的实时快照时，较早详情响应不会回退版本", async () => {
  const old = deferred<Workspace>(); vi.mocked(agentApi.workspace).mockReturnValue(old.promise);
  render(<DesignChat />);
  await waitFor(() => expect(Stream.instances).toHaveLength(1));
  const newer = workspace(); newer.project.name = "合成新快照";
  act(() => Stream.instances[0].snapshot(newer));
  await act(async () => old.resolve(workspace()));
  expect(screen.getByRole("heading", { name: "合成新快照" })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "合成任务 1" })).toBeNull();
});

it("发送失败保留输入，原内容重试保持请求编号与基础版本", async () => {
  vi.mocked(agentApi.sendMessage).mockRejectedValueOnce(new Error("结果待确认，请先核对记录"));
  render(<DesignChat />);
  fireEvent.change(await screen.findByLabelText("发消息给设计助手"), { target: { value: "保留领口，裙摆加长" } });
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await screen.findByText("结果待确认，请先核对记录");
  expect((screen.getByLabelText("发消息给设计助手") as HTMLTextAreaElement).value).toBe("保留领口，裙摆加长");
  const first = vi.mocked(agentApi.sendMessage).mock.calls[0][1];
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(agentApi.sendMessage).toHaveBeenCalledTimes(2));
  expect(vi.mocked(agentApi.sendMessage).mock.calls[1][1]).toEqual(first);
  expect(first.authorized).toBe(false);
});

it("能力未启用时不能恢复中断的图片批次", async () => {
  const state = workspace(); state.versions = [{ ...candidate(), task_id: "batch", design_count: 2 }];
  state.tasks = [{ id: "batch", mode: "design", status: "interrupted", design_count: 2, outcome: null, questions: [], reasoning_calls: 0, image_calls: 1, reserved_cost_fen: 0, max_cost_fen: null, steps: [{ id: "step", tool: "generate_design", status: "unknown" }], observations: [] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  const resume = await screen.findByRole("button", { name: "继续生成剩余 1 款" }) as HTMLButtonElement;
  expect(resume.disabled).toBe(true); fireEvent.click(resume); expect(agentApi.control).not.toHaveBeenCalled();
});

it("弹窗Tab在内部循环，Escape归焦并保留未保存修改", async () => {
  const state = workspace(); state.versions = [candidate()]; vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<DesignChat />);
  const trigger = await screen.findByRole("button", { name: "修改这款" }); fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog");
  const region = within(dialog).getByLabelText("只修改哪个部位？");
  const feedback = within(dialog).getByLabelText("希望怎么修改？");
  expect(document.activeElement).toBe(region);
  fireEvent.change(region, { target: { value: "领口" } }); fireEvent.change(feedback, { target: { value: "保留结构，加深一厘米" } });
  const save = within(dialog).getByRole("button", { name: "保存修改要求" }); save.focus(); fireEvent.keyDown(save, { key: "Tab" }); expect(document.activeElement).toBe(region);
  fireEvent.keyDown(region, { key: "Tab", shiftKey: true }); expect(document.activeElement).toBe(save);
  fireEvent.keyDown(save, { key: "Escape" }); expect(screen.queryByRole("dialog")).toBeNull(); expect(document.activeElement).toBe(trigger);
  fireEvent.click(trigger); expect((await screen.findByLabelText("希望怎么修改？") as HTMLTextAreaElement).value).toBe("保留结构，加深一厘米");
  expect(agentApi.revise).not.toHaveBeenCalled();
});

it("只读方案页可以查看打样细节，但不能编辑或保存", async () => {
  const state = workspace(); state.versions = [{ ...candidate(), status: "confirmed" }]; vi.mocked(agentApi.workspace).mockResolvedValue(state);
  render(<SelectedDesignPage projectId={1} versionId="v1" readOnly />);
  fireEvent.click(await screen.findByRole("button", { name: "查看打样细节" }));
  expect((screen.getByLabelText(/面料与辅料/) as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "自动整理草稿" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "保存打样资料草稿" }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByText(/提交到 CMS/)).toBeNull();
  expect(agentApi.saveSamplingSheet).not.toHaveBeenCalled();
});

it("切换独立方案任务后，旧任务晚响应不显示", async () => {
  const old = deferred<Workspace>();
  const next = workspace(2); next.versions = [{ ...candidate("v2"), status: "confirmed" }];
  vi.mocked(agentApi.workspace).mockImplementation(id => id === 1 ? old.promise : Promise.resolve(next));
  const view = render(<SelectedDesignPage projectId={1} versionId="v1" />);
  view.rerender(<SelectedDesignPage projectId={2} versionId="v2" />);
  await screen.findByText("合成任务 2");
  const previous = workspace(); previous.versions = [candidate()];
  await act(async () => old.resolve(previous));
  expect(screen.getByText("合成任务 2")).toBeTruthy(); expect(screen.queryByText("合成任务 1")).toBeNull();
});

it("3D明确不可用；即使能力开启，管理者仍不能生成", async () => {
  const view = render(<Model3DPanel version={candidate()} title="合成方案" caps={capabilities} />);
  expect(screen.getByText(/3D 服务未启用/)).toBeTruthy(); expect(agentApi.generate3D).not.toHaveBeenCalled();
  view.rerender(<Model3DPanel version={candidate()} title="合成方案" caps={{ ...capabilities, three_d: { ...capabilities.three_d!, enabled: true } }} readOnly />);
  const button = screen.getByRole("button", { name: /制作这款的 3D 预览/ }) as HTMLButtonElement;
  expect(button.disabled).toBe(true); fireEvent.click(button); expect(agentApi.generate3D).not.toHaveBeenCalled();
});

it("未启用技术图服务不允许发起模型调用", () => {
  render(<TechnicalFlatPanel versionId="v1" caps={capabilities} onSaved={async () => undefined} />);
  expect(screen.getByText(/技术平面图生成服务未启用/)).toBeTruthy();
  const button = screen.getByRole("button", { name: "生成草图" }) as HTMLButtonElement;
  expect(button.disabled).toBe(true); fireEvent.click(button); expect(agentApi.generateTechnicalFlat).not.toHaveBeenCalled();
});

it("受权图片失败有明确反馈，不回退到公开token图", () => {
  render(<Model3DPanel version={candidate()} title="合成方案" caps={capabilities} />);
  const picture = screen.getByRole("img", { name: "合成方案" });
  expect(picture.getAttribute("src")).toBe(agentApi.image("v1", "version"));
  fireEvent.error(picture);
  expect(screen.getByRole("alert").textContent).toContain("访问权限或连接可能已变化");
  expect(document.querySelector('img[src*="design-public"]')).toBeNull();
});

it("对话草稿绑定开始编辑时的要求版本，版本变化须显式核对", async () => {
  render(<DesignChat />);
  fireEvent.change(await screen.findByLabelText("发消息给设计助手"), { target: { value: "领口稍微加深" } });
  const changed = workspace(); changed.specs = [{ ...changed.specs[0], id: "s2" }]; changed.head.spec_id = "s2";
  act(() => Stream.instances[0].snapshot(changed));
  expect(screen.getByText(/未发送的内容仍保留/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByLabelText("发消息给设计助手") as HTMLTextAreaElement).value).toBe("领口稍微加深");
  fireEvent.click(screen.getByRole("button", { name: "已核对，按当前版本发送" }));
  fireEvent.click(screen.getByRole("button", { name: "发送" }));
  await waitFor(() => expect(agentApi.sendMessage).toHaveBeenCalledWith(1, expect.objectContaining({ expected_spec_id: "s2", text: "领口稍微加深" })));
});

it("修改弹窗在409后保稿，基础要求变化不能自动挪到新版本", async () => {
  const state = workspace(); state.versions = [candidate()]; vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.revise).mockRejectedValue(new Error("409 要求版本冲突"));
  render(<DesignChat />);
  fireEvent.click(await screen.findByRole("button", { name: "修改这款" }));
  fireEvent.change(screen.getByLabelText("只修改哪个部位？"), { target: { value: "领口" } });
  fireEvent.change(screen.getByLabelText("希望怎么修改？"), { target: { value: "领口加深一厘米" } });
  fireEvent.click(screen.getByRole("button", { name: "保存修改要求" }));
  await screen.findByText("409 要求版本冲突");
  expect((screen.getByLabelText("希望怎么修改？") as HTMLTextAreaElement).value).toBe("领口加深一厘米");
  const newer = workspace(); newer.versions = [candidate()]; newer.specs.push({ ...newer.specs[0], id: "s2" }); newer.head.spec_id = "s2";
  act(() => Stream.instances[0].snapshot(newer));
  expect(screen.getByText(/修改内容仍保留/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "保存修改要求" }) as HTMLButtonElement).disabled).toBe(true);
  expect(agentApi.revise).toHaveBeenCalledTimes(1);
  expect(agentApi.revise).toHaveBeenCalledWith("v1", "领口加深一厘米", "领口", "s1");
});

it("风格规划的新服务器版本不会静默覆盖本地调整", async () => {
  const state = workspace(); state.specs[0].status = "confirmed"; state.head.style_plan_id = "plan1";
  state.style_plans = [{ id: "plan1", spec_id: "s1", source_fingerprint: "f1", category: "apparel", status: "draft", directions: [{ id: "d1", name: "原始方向", selected: true, theme: "主题", rationale: "理由", explore: "变化", color_story: "颜色", structure: "结构", material_story: "材料" }] }];
  vi.mocked(agentApi.workspace).mockResolvedValue(state); render(<DesignChat />);
  fireEvent.click(await screen.findByText("调整这条方向"));
  fireEvent.change(screen.getByLabelText("方向名称"), { target: { value: "我的未保存方向" } });
  const next = { ...state, head: { ...state.head, style_plan_id: "plan2" }, style_plans: [{ ...state.style_plans[0], id: "plan2", directions: [{ ...state.style_plans[0].directions[0], name: "服务端新方向" }] }] };
  act(() => Stream.instances[0].snapshot(next));
  expect(screen.getByText(/服务端方向版本已变化/)).toBeTruthy();
  expect((screen.getByLabelText("方向名称") as HTMLInputElement).value).toBe("我的未保存方向");
  expect((screen.getByRole("button", { name: "保存方向调整" }) as HTMLButtonElement).disabled).toBe(true);
  expect(agentApi.reviseStylePlan).not.toHaveBeenCalled();
});

it("方向保存回执未完成时不能确认，完成后只确认保存得到的新版本", async () => {
  const state = workspace(); state.specs[0].status = "confirmed"; state.head.style_plan_id = "plan1";
  state.style_plans = [{ id: "plan1", spec_id: "s1", source_fingerprint: "f1", category: "apparel", status: "draft", directions: [1, 2, 3].map(index => ({ id: `d${index}`, name: `方向${index}`, selected: true, theme: "主题", rationale: "理由", explore: "变化", color_story: "颜色", structure: "结构", material_story: "材料" })) }];
  const revisedPlan = { ...state.style_plans[0], id: "plan2", directions: state.style_plans[0].directions.map((direction, index) => ({ ...direction, selected: index < 2 })) };
  const receipt = deferred<typeof revisedPlan>();
  vi.mocked(agentApi.workspace).mockResolvedValue(state);
  vi.mocked(agentApi.reviseStylePlan).mockReturnValue(receipt.promise);
  vi.mocked(agentApi.capabilities).mockResolvedValue({ ...capabilities, understand: true, design: true });
  render(<DesignChat />);
  fireEvent.click((await screen.findAllByRole("checkbox", { name: "保留这个方向" }))[2]);
  fireEvent.click(screen.getByRole("button", { name: "保存方向调整" }));
  await waitFor(() => expect(agentApi.reviseStylePlan).toHaveBeenCalledWith("plan1", revisedPlan.directions));
  const unavailable = screen.getByRole("button", { name: "确认这 2 个方向" }) as HTMLButtonElement;
  expect(unavailable.disabled).toBe(true); fireEvent.click(unavailable);
  expect(agentApi.confirmStylePlan).not.toHaveBeenCalled();
  vi.mocked(agentApi.workspace).mockResolvedValue({ ...state, head: { ...state.head, style_plan_id: "plan2" }, style_plans: [revisedPlan] });
  await act(async () => receipt.resolve(revisedPlan));
  await waitFor(() => expect((screen.getByRole("button", { name: "确认这 2 个方向" }) as HTMLButtonElement).disabled).toBe(false));
  vi.mocked(agentApi.confirmStylePlan).mockResolvedValue({ ...revisedPlan, status: "confirmed" });
  vi.mocked(agentApi.workspace).mockResolvedValue({ ...state, head: { ...state.head, style_plan_id: "plan2" }, style_plans: [{ ...revisedPlan, status: "confirmed" }] });
  fireEvent.click(screen.getByRole("button", { name: "确认这 2 个方向" }));
  await waitFor(() => expect(agentApi.confirmStylePlan).toHaveBeenCalledWith("plan2"));
  expect(agentApi.confirmStylePlan).toHaveBeenCalledTimes(1);
  await screen.findByRole("button", { name: "生成 2 款" });
});


it("工作区与独立方案页以当前授权项目打开折叠来源，不读取URL伪造来源", async () => {
  window.history.replaceState(null, "", "/?project=1&handoff=untrusted");
  const state = workspace(); state.versions = [candidate()]; vi.mocked(agentApi.workspace).mockResolvedValue(state);
  const chat = render(<DesignChat />);
  const summary = await screen.findByText("查看完整上游需求与设计提示词");
  expect(screen.queryByTestId("project-source-probe")).toBeNull();
  fireEvent.click(summary);
  expect((await screen.findByTestId("project-source-probe")).textContent).toBe("来源项目 1");
  chat.unmount();
  render(<SelectedDesignPage projectId={1} versionId="v1" />);
  const selectedSummary = await screen.findByText("查看完整上游需求与设计提示词");
  expect(screen.queryByTestId("project-source-probe")).toBeNull();
  fireEvent.click(selectedSummary);
  expect((await screen.findByTestId("project-source-probe")).textContent).toBe("来源项目 1");
});
