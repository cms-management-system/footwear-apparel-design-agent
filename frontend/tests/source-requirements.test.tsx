import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SourceRequirements from "@/components/team/SourceRequirements";
import ProjectSource from "@/components/team/ProjectSource";
import { workflowApi, type Handoff } from "@/lib/design-workflow";
import { ApiError } from "@/lib/team-api";

vi.mock("@/lib/design-workflow", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/design-workflow")>();
  return { ...original, workflowApi: { ...original.workflowApi, list: vi.fn(), detail: vi.fn(), operation: vi.fn() }, executeAction: vi.fn() };
});

const longPrompt = `完整开头${"保持面料与结构的一致性。".repeat(650)}完整末尾不可截断`;
const binding = () => ({ package: { id: "source-package-a", version: "source-v3", requirement_base64: "ENCODED_REQUIREMENT_COPY", prompt_base64: "ENCODED_PROMPT_COPY" }, requirement: { requirement_desc: "上游批准的完整产品要求", constraints: ["仅使用已批准面料", "保留可追溯版本"] }, prompt: { positive_prompt: longPrompt, avoid_items: ["未经批准的图案"] }, receipt: { id: "source-receipt-a", status: "received" } });
const item = (projectId: number, id = `handoff-${projectId}`): Handoff => ({ id, package_id: `package-${projectId}`, version: "v1", package: { title: "授权任务摘要" }, project_id: projectId, status: "assigned", assignee: "designer-a", submitted_version_id: null, manager_note: "", created_at: "2026-10-10T07:00:00Z", revision: 1, allowed_actions: [] });
const detail = (projectId: number, overrides: Record<string, unknown> = {}) => ({ ...item(projectId), source_binding: binding(), ...overrides });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((success, failure) => { resolve = success; reject = failure; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.mocked(workflowApi.list).mockReset();
  vi.mocked(workflowApi.detail).mockReset();
  vi.mocked(workflowApi.operation).mockReset();
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("Unexpected request outside authorized read mocks"))));
  window.history.replaceState(null, "", "/?project=1");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("完整上游正文的只读展示", () => {
  it("完整展示超过 4000 字的解码需求与 prompt，隐藏重复 base64 传输副本", () => {
    const source = binding();
    render(<SourceRequirements binding={source} />);
    expect(screen.getByText("上游批准的完整产品要求")).toBeTruthy();
    expect(screen.getByText(longPrompt).textContent).toBe(longPrompt);
    expect(screen.getByText("仅使用已批准面料")).toBeTruthy();
    expect(screen.getByText("未经批准的图案")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/ENCODED_|requirement_base64|prompt_base64/);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByText("来源包与版本信息").closest("details")?.open).toBe(false);
    expect(screen.getByText("技术接收回执").closest("details")?.open).toBe(false);
    expect(document.body.textContent).toContain("source-v3");
    expect(document.body.textContent).toContain("source-receipt-a");
    expect(source.package.prompt_base64).toBe("ENCODED_PROMPT_COPY");
  });

  it.each([undefined, null, "invalid-binding", []])("来源绑定缺失或无效时明确说明，不能拿摘要补成正文：%j", source => {
    render(<SourceRequirements binding={source} />);
    expect(screen.getByText(/当前任务未提供完整上游来源绑定/)).toBeTruthy();
    expect(screen.queryByText("完整设计提示词", { selector: "h3" })).toBeNull();
  });

  it("缺解码正文时指出缺失，不展示 package 里的 base64", () => {
    render(<SourceRequirements binding={{ package: binding().package, requirement: null, prompt: null }} />);
    expect(screen.getByText("完整产品需求尚未提供，请核对上游来源。")).toBeTruthy();
    expect(screen.getByText("完整设计提示词尚未提供，请核对上游来源。")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/ENCODED_|requirement_base64|prompt_base64/);
  });

  it("正文中的 HTML 保持纯文字，不产生图片、脚本或外链", () => {
    const untrusted = '<img src="https://example.invalid/private" onerror="alert(1)"><script>private()</script>';
    const view = render(<SourceRequirements binding={{ requirement: untrusted, prompt: "https://example.invalid/prompt" }} />);
    expect(screen.getByText(untrusted)).toBeTruthy();
    expect(view.container.querySelector("img, script, a")).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("按授权任务查找项目来源", () => {
  it("分页读取授权列表，再核对详情的项目绑定，展示完整上游正文", async () => {
    const firstPage = Array.from({ length: 20 }, (_, index) => item(index + 100));
    vi.mocked(workflowApi.list).mockResolvedValueOnce({ items: firstPage, total: 21 }).mockResolvedValueOnce({ items: [item(1)], total: 21 });
    vi.mocked(workflowApi.detail).mockResolvedValue(detail(1));
    render(<ProjectSource projectId={1} />);
    expect(screen.getByRole("status").textContent).toContain("正在核对");
    expect(screen.queryByText(longPrompt)).toBeNull();
    await screen.findByText(longPrompt);
    expect(workflowApi.list).toHaveBeenNthCalledWith(1, 0, expect.any(AbortSignal));
    expect(workflowApi.list).toHaveBeenNthCalledWith(2, 20, expect.any(AbortSignal));
    expect(workflowApi.detail).toHaveBeenCalledWith("handoff-1", expect.any(AbortSignal));
    expect(workflowApi.operation).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("URL 中的其他 handoff 不可替代服务端当前项目绑定", async () => {
    window.history.replaceState(null, "", "/?project=1&handoff=unauthorized-other");
    vi.mocked(workflowApi.list).mockResolvedValue({ items: [item(1)], total: 1 });
    vi.mocked(workflowApi.detail).mockResolvedValue(detail(1));
    render(<ProjectSource projectId={1} />);
    await screen.findByText(longPrompt);
    expect(workflowApi.detail).toHaveBeenCalledTimes(1);
    expect(workflowApi.detail).toHaveBeenCalledWith("handoff-1", expect.any(AbortSignal));
  });

  it("详情项目绑定不一致时拒绝显示来源正文", async () => {
    vi.mocked(workflowApi.list).mockResolvedValue({ items: [item(1)], total: 1 });
    vi.mocked(workflowApi.detail).mockResolvedValue(detail(2, { id: "handoff-1" }));
    render(<ProjectSource projectId={1} />);
    expect((await screen.findByRole("alert")).textContent).toContain("需求与当前设计项目不一致");
    expect(screen.queryByText(longPrompt)).toBeNull();
    expect(screen.queryByText("上游批准的完整产品要求")).toBeNull();
  });

  it("详情任务 ID 不一致时不显示其他任务正文", async () => {
    vi.mocked(workflowApi.list).mockResolvedValue({ items: [item(1)], total: 1 });
    vi.mocked(workflowApi.detail).mockResolvedValue(detail(1, { id: "other-handoff" }));
    render(<ProjectSource projectId={1} />);
    await screen.findByRole("alert");
    expect(screen.queryByText(longPrompt)).toBeNull();
  });

  it("授权列表无匹配时提示返回我的任务，不创建或猜测来源", async () => {
    vi.mocked(workflowApi.list).mockResolvedValue({ items: [item(2)], total: 1 });
    render(<ProjectSource projectId={1} />);
    await screen.findByText(/没有找到当前项目对应的授权任务/);
    expect(screen.getByRole("link", { name: "返回我的任务 →" }).getAttribute("href")).toBe("/handoffs");
    expect(workflowApi.detail).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("未选择项目时不读取私有来源", () => {
    render(<ProjectSource projectId={null} />);
    expect(screen.getByRole("link", { name: "返回我的任务 →" })).toBeTruthy();
    expect(workflowApi.list).not.toHaveBeenCalled();
    expect(workflowApi.detail).not.toHaveBeenCalled();
  });

  it("详情没有 source_binding 时显示明确缺失，不以列表摘要代替", async () => {
    vi.mocked(workflowApi.list).mockResolvedValue({ items: [item(1)], total: 1 });
    vi.mocked(workflowApi.detail).mockResolvedValue(item(1));
    render(<ProjectSource projectId={1} />);
    await screen.findByText(/当前任务未提供完整上游来源绑定/);
    expect(screen.queryByText("授权任务摘要")).toBeNull();
  });

  it("切换项目时立即隐藏旧来源并拒收旧详情晚响应", async () => {
    const oldDetail = deferred<Handoff>();
    vi.mocked(workflowApi.list).mockResolvedValueOnce({ items: [item(1)], total: 1 }).mockResolvedValueOnce({ items: [item(2)], total: 1 });
    vi.mocked(workflowApi.detail).mockReturnValueOnce(oldDetail.promise).mockResolvedValueOnce(detail(2, { source_binding: { requirement: "项目乙的完整需求", prompt: "项目乙的完整提示词" } }));
    const view = render(<ProjectSource projectId={1} />);
    await waitFor(() => expect(workflowApi.detail).toHaveBeenCalledTimes(1));
    const oldSignal = vi.mocked(workflowApi.detail).mock.calls[0][1];
    view.rerender(<ProjectSource projectId={2} />);
    expect(oldSignal?.aborted).toBe(true);
    await screen.findByText("项目乙的完整提示词");
    await act(async () => oldDetail.resolve(detail(1)));
    expect(screen.getByText("项目乙的完整需求")).toBeTruthy();
    expect(screen.queryByText(longPrompt)).toBeNull();
  });

  it("已展示的来源在项目切换后加载期间不残留", async () => {
    const nextList = deferred<{ items: Handoff[]; total: number }>();
    vi.mocked(workflowApi.list).mockResolvedValueOnce({ items: [item(1)], total: 1 }).mockReturnValueOnce(nextList.promise);
    vi.mocked(workflowApi.detail).mockResolvedValue(detail(1));
    const view = render(<ProjectSource projectId={1} />);
    await screen.findByText(longPrompt);
    view.rerender(<ProjectSource projectId={2} />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryByText(longPrompt)).toBeNull();
    await act(async () => nextList.resolve({ items: [], total: 0 }));
    await screen.findByText(/没有找到当前项目对应的授权任务/);
  });

  it("卸载后旧列表响应不能继续读取详情", async () => {
    const oldList = deferred<{ items: Handoff[]; total: number }>();
    vi.mocked(workflowApi.list).mockReturnValue(oldList.promise);
    const view = render(<ProjectSource projectId={1} />);
    const signal = vi.mocked(workflowApi.list).mock.calls[0][1];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => oldList.resolve({ items: [item(1)], total: 1 }));
    expect(workflowApi.detail).not.toHaveBeenCalled();
  });

  it("读取失败与无匹配分开显示，显式重试只重新读取", async () => {
    vi.mocked(workflowApi.list).mockRejectedValueOnce(new ApiError("NETWORK_UNAVAILABLE", "上游要求暂时无法读取")).mockResolvedValueOnce({ items: [item(1)], total: 1 });
    vi.mocked(workflowApi.detail).mockResolvedValue(detail(1));
    render(<ProjectSource projectId={1} />);
    expect((await screen.findByRole("alert")).textContent).toBe("上游要求暂时无法读取");
    expect(screen.queryByText(/没有找到当前项目对应的授权任务/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新读取上游要求" }));
    await screen.findByText(longPrompt);
    expect(workflowApi.list).toHaveBeenCalledTimes(2);
    expect(fetch).not.toHaveBeenCalled();
  });
});
