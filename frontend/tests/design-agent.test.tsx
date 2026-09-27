import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DesignAgentPanel from "@/components/design/DesignAgentPanel";
import { activeTask, agentApi, blankSpec, statusLabel, type Workspace } from "@/lib/agent-api";

vi.mock("@/lib/agent-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/agent-api")>();
  return { ...original, agentApi: { ...original.agentApi, capabilities: vi.fn(), projects: vi.fn(), workspace: vi.fn(), save: vi.fn(), confirmSpec: vi.fn(), create: vi.fn(), upload: vi.fn(), submit: vi.fn() } };
});
const data = (): Workspace => ({ project: { id: 1, name: "测试设计" }, head: { spec_id: "s1" }, assets: [], tasks: [], versions: [], specs: [{ id: "s1", status: "draft", fingerprint: "f1", spec: { ...blankSpec(), intent: "方领连衣裙" } }] });
beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:test"), revokeObjectURL: vi.fn() }));
  window.history.replaceState(null, "", "/design?project=1");
  vi.mocked(agentApi.projects).mockResolvedValue({ items: [{ id: 1, name: "测试设计" }] });
  vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: false, design: false, vision_service: "待配置", image_service: "测试通道", quality_status: "待验", note: "", monthly_allocation_fen: 0 });
  vi.mocked(agentApi.workspace).mockResolvedValue(data());
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
describe("设计共创的确认与恢复", () => {
  it("未接通模型时可查看要求，但不冒充可生成", async () => {
    render(<DesignAgentPanel />);
    await screen.findByDisplayValue("方领连衣裙");
    expect((screen.getByRole("button", { name: "按确认要求开始设计" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("共创工作区已开放 · 视觉模型待接通")).toBeTruthy();
  });
  it("编辑后不能跳过保存直接确认，保存失败保留输入", async () => {
    vi.mocked(agentApi.save).mockRejectedValue(new Error("要求单已有新版本"));
    render(<DesignAgentPanel />);
    fireEvent.change(await screen.findByDisplayValue("方领连衣裙"), { target: { value: "保留领口，只改袖子" } });
    expect((screen.getByRole("button", { name: "确认这份要求" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "保存要求单" }));
    await screen.findByText(/要求单已有新版本/);
    expect(screen.getByDisplayValue("保留领口，只改袖子")).toBeTruthy();
    expect(agentApi.confirmSpec).not.toHaveBeenCalled();
  });
  it("从服务端恢复等待补充任务并锁住要求单", async () => {
    const snapshot = data();
    snapshot.tasks = [{ id: "t1", status: "awaiting_input", outcome: null, questions: ["哪张图决定领口？"], reasoning_calls: 2, image_calls: 0, reserved_cost_fen: 2, max_cost_fen: 500, steps: [], observations: [] }];
    vi.mocked(agentApi.workspace).mockResolvedValue(snapshot);
    render(<DesignAgentPanel />);
    await screen.findByText("哪张图决定领口？");
    expect((screen.getByDisplayValue("方领连衣裙") as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "取消后续步骤" })).toBeTruthy();
  });
  it("未知状态不显示成功", () => {
    expect(statusLabel("new_unknown_state")).toBe("状态待核对");
    expect(activeTask({ ...data().tasks[0], status: "cancelled" })).toBe(false);
  });
});

describe("统一的开始理解入口", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
    vi.mocked(agentApi.create).mockResolvedValue({ id: 1 });
    vi.mocked(agentApi.save).mockResolvedValue(data().specs[0]);
    vi.mocked(agentApi.submit).mockResolvedValue({ id: "t1" } as Workspace["tasks"][number]);
  });
  it("无需项目名，未开通时保存文字且不提交模型任务", async () => {
    render(<DesignAgentPanel />);
    const input = await screen.findByLabelText("说说你要做什么样的衣服");
    expect(screen.queryByLabelText("新设计名称")).toBeNull();
    expect(agentApi.create).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "保留方领和腰线" } });
    fireEvent.click(screen.getByRole("button", { name: "开始理解" }));
    await screen.findByText(/本次没有执行理解/);
    expect(agentApi.create).toHaveBeenCalledWith("保留方领和腰线");
    expect(agentApi.save).toHaveBeenCalledWith(1, expect.objectContaining({ intent: "保留方领和腰线" }), null);
    expect(agentApi.submit).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe("?project=1");
  });
  it("已开通时直接开始理解，保留额度且不自动确认要求", async () => {
    vi.mocked(agentApi.capabilities).mockResolvedValue({ understand: true, design: false, vision_service: "test.example", image_service: "test", quality_status: "待验", note: "", monthly_allocation_fen: 2000 });
    render(<DesignAgentPanel />);
    const input = await screen.findByLabelText("说说你要做什么样的衣服");
    fireEvent.change(input, { target: { value: "方领连衣裙" } });
    const start = screen.getByRole("button", { name: "开始理解" }) as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByText(/调整额度/)).toBeNull();
    fireEvent.change(input, { target: { value: "方领连衣裙，保留腰线" } });
    expect(start.disabled).toBe(false);
    fireEvent.click(start);
    await waitFor(() => expect(agentApi.submit).toHaveBeenCalledWith(1, "s1", "understand", expect.any(String)));
    expect(agentApi.confirmSpec).not.toHaveBeenCalled();
  });
  it("上传失败保留素材与文字，重试复用项目和已上传素材，保留图片用途", async () => {
    vi.mocked(agentApi.upload).mockResolvedValueOnce({ id: "a1", name: "sketch.png", width: 100, height: 100 })
      .mockRejectedValueOnce(new Error("面料图上传中断"))
      .mockResolvedValueOnce({ id: "a2", name: "fabric.png", width: 100, height: 100 });
    render(<DesignAgentPanel />);
    fireEvent.change(await screen.findByLabelText("说说你要做什么样的衣服"), { target: { value: "草图配这块面料" } });
    fireEvent.change(screen.getByLabelText(/添加草图、面料照片/), { target: { files: [new File(["test"], "sketch.png", { type: "image/png" }), new File(["test"], "fabric.png", { type: "image/png" })] } });
    fireEvent.change(screen.getAllByLabelText("图片用途")[1], { target: { value: "fabric" } });
    fireEvent.click(screen.getByRole("button", { name: "开始理解" }));
    await screen.findByText(/面料图上传中断/);
    expect(screen.getByDisplayValue("草图配这块面料")).toBeTruthy();
    expect(screen.getByText("fabric.png")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "开始理解" }));
    await screen.findByText(/本次没有执行理解/);
    expect(agentApi.create).toHaveBeenCalledTimes(1);
    expect(agentApi.upload).toHaveBeenCalledTimes(3);
    expect(agentApi.save).toHaveBeenCalledWith(1, expect.objectContaining({ references: [
      expect.objectContaining({ asset_id: "a1", role: "structure" }), expect.objectContaining({ asset_id: "a2", role: "fabric" }),
    ] }), null);
  });
  it("超过六张图片时拒绝并保持尚未上传", async () => {
    render(<DesignAgentPanel />);
    await screen.findByLabelText("说说你要做什么样的衣服");
    fireEvent.change(screen.getByLabelText(/添加草图、面料照片/), { target: { files: Array.from({ length: 7 }, (_, i) => new File(["test"], `${i}.png`, { type: "image/png" })) } });
    await screen.findByText(/最多添加 6 张参考图/);
    expect(agentApi.upload).not.toHaveBeenCalled();
    expect(agentApi.create).not.toHaveBeenCalled();
  });
});

it("保存回执丢失时恢复服务端同一份要求，不重复创建或丢失输入", async () => {
  window.history.replaceState(null, "", "/");
  vi.mocked(agentApi.create).mockResolvedValue({ id: 1 });
  vi.mocked(agentApi.save).mockRejectedValue(new Error("连接超时"));
  render(<DesignAgentPanel />);
  fireEvent.change(await screen.findByLabelText("说说你要做什么样的衣服"), { target: { value: "方领连衣裙" } });
  fireEvent.click(screen.getByRole("button", { name: "开始理解" }));
  await screen.findByText(/本次没有执行理解/);
  expect(agentApi.create).toHaveBeenCalledTimes(1);
  expect(agentApi.save).toHaveBeenCalledTimes(1);
  expect(agentApi.submit).not.toHaveBeenCalled();
});
