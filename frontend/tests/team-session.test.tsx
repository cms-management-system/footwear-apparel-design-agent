import { StrictMode, useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { announceSessionChange, TeamBoundary } from "@/components/team/TeamSession";
import { ApiError, bindDesignContext, designContextKey, jsonBody, teamApi, teamRequest, type TeamUser } from "@/lib/team-api";

vi.mock("@/lib/team-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/team-api")>();
  return { ...original, teamApi: { ...original.teamApi, me: vi.fn(), logout: vi.fn(), login: vi.fn() } };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((success, failure) => { resolve = success; reject = failure; });
  return { promise, resolve, reject };
}
const employee = (overrides: Partial<TeamUser> = {}): TeamUser => ({ username: "designer-a", display_name: "设计员工甲", role: "designer", integration_mode: "managed", auth_context_id: "context-a", instance_id: "design-test", scope_id: "scope-test", ...overrides });
function PrivateWorkspace({ user }: { user: TeamUser }) {
  const [draft, setDraft] = useState("");
  return <section aria-label="私有工作区"><h1>{user.display_name}的任务</h1><label>未保存设计草稿<textarea value={draft} onChange={event => setDraft(event.target.value)} /></label></section>;
}
function renderWorkspace() {
  return render(<TeamBoundary>{user => <PrivateWorkspace user={user} />}</TeamBoundary>);
}
function focusWindow() { fireEvent(window, new Event("focus")); }

beforeEach(() => {
  vi.mocked(teamApi.me).mockReset();
  vi.mocked(teamApi.login).mockReset();
  vi.mocked(teamApi.logout).mockReset();
  bindDesignContext(null);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  bindDesignContext(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("工作区身份边界", () => {
  it("首次身份加载只显示核验状态，不挂载私有内容", async () => {
    const initial = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockReturnValue(initial.promise);
    renderWorkspace();
    expect(screen.getByRole("status").textContent).toContain("正在确认工作区身份");
    expect(screen.queryByRole("region", { name: "私有工作区", hidden: true })).toBeNull();
    expect(screen.queryByLabelText("未保存设计草稿")).toBeNull();
    await act(async () => initial.resolve(employee()));
    expect(screen.getByRole("heading", { name: "设计员工甲的任务" })).toBeTruthy();
    expect(designContextKey()).toBe("context-a");
  });

  it("窗口重新聚焦时隐藏私有区域，身份不变确认后保留未保存草稿", async () => {
    const recheck = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockResolvedValueOnce(employee()).mockReturnValueOnce(recheck.promise);
    renderWorkspace();
    const input = await screen.findByRole("textbox", { name: "未保存设计草稿" });
    fireEvent.change(input, { target: { value: "甲的未保存袖型" } });
    focusWindow();
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "私有工作区" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "未保存设计草稿" })).toBeNull();
    expect(designContextKey()).not.toBe("context-a");
    await act(async () => recheck.resolve(employee()));
    expect((screen.getByRole("textbox", { name: "未保存设计草稿" }) as HTMLTextAreaElement).value).toBe("甲的未保存袖型");
    expect(designContextKey()).toBe("context-a");
    expect(teamApi.login).not.toHaveBeenCalled();
    expect(teamApi.logout).not.toHaveBeenCalled();
  });

  it.each([
    { username: "designer-b", display_name: "设计员工乙" },
    { role: "manager" as const },
    { auth_context_id: "context-new-session" },
    { scope_id: "scope-other" },
    { instance_id: "design-other" },
  ])("身份边界变化会重挂工作区，旧草稿不交给新身份：%j", async change => {
    const recheck = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockResolvedValueOnce(employee()).mockReturnValueOnce(recheck.promise);
    renderWorkspace();
    fireEvent.change(await screen.findByRole("textbox", { name: "未保存设计草稿" }), { target: { value: "只属于原身份的草稿" } });
    focusWindow();
    await act(async () => recheck.resolve(employee(change)));
    expect((screen.getByRole("textbox", { name: "未保存设计草稿" }) as HTMLTextAreaElement).value).toBe("");
    expect(screen.queryByDisplayValue("只属于原身份的草稿")).toBeNull();
  });

  it("旧 me 忽略取消后迟到成功也不能覆盖新身份", async () => {
    const older = deferred<TeamUser>();
    const latest = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise);
    renderWorkspace();
    const oldSignal = vi.mocked(teamApi.me).mock.calls[0][0];
    focusWindow();
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => latest.resolve(employee({ username: "designer-b", display_name: "设计员工乙", auth_context_id: "context-b" })));
    await act(async () => older.resolve(employee()));
    expect(screen.getByRole("heading", { name: "设计员工乙的任务" })).toBeTruthy();
    expect(screen.queryByText("设计员工甲的任务")).toBeNull();
    expect(designContextKey()).toBe("context-b");
  });

  it("旧 me 迟到 401 不得清除较新已确认身份", async () => {
    const older = deferred<TeamUser>();
    const latest = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise);
    renderWorkspace();
    focusWindow();
    await act(async () => latest.resolve(employee({ auth_context_id: "context-latest" })));
    await act(async () => older.reject(new ApiError("SESSION_EXPIRED", "旧会话已失效", 401)));
    expect(screen.getByRole("heading", { name: "设计员工甲的任务" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(designContextKey()).toBe("context-latest");
  });

  it("身份核验 401 清除原私有工作区，显式重新连接后才恢复", async () => {
    vi.mocked(teamApi.me).mockResolvedValueOnce(employee()).mockRejectedValueOnce(new ApiError("SESSION_EXPIRED", "登录已失效", 401)).mockResolvedValueOnce(employee({ username: "designer-b", display_name: "设计员工乙", auth_context_id: "context-b" }));
    renderWorkspace();
    fireEvent.change(await screen.findByRole("textbox", { name: "未保存设计草稿" }), { target: { value: "原身份私有草稿" } });
    focusWindow();
    await screen.findByRole("heading", { name: "请重新登录" });
    expect(screen.queryByRole("region", { name: "私有工作区", hidden: true })).toBeNull();
    expect(screen.queryByDisplayValue("原身份私有草稿")).toBeNull();
    expect(screen.getByRole("link", { name: "前往登录" }).getAttribute("href")).toBe("/login");
    expect(designContextKey()).not.toBe("context-a");
    expect(teamApi.me).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    await screen.findByRole("heading", { name: "设计员工乙的任务" });
    expect((screen.getByRole("textbox", { name: "未保存设计草稿" }) as HTMLTextAreaElement).value).toBe("");
    expect(teamApi.me).toHaveBeenCalledTimes(3);
    expect(teamApi.login).not.toHaveBeenCalled();
    expect(teamApi.logout).not.toHaveBeenCalled();
  });

  it("身份错误不展开任意对象，重新连接成功后移除错误", async () => {
    vi.mocked(teamApi.me).mockRejectedValueOnce({ trace: "private-provider-trace", input: "private-user-draft" }).mockResolvedValueOnce(employee());
    renderWorkspace();
    await screen.findByRole("heading", { name: "暂时无法打开工作区" });
    expect(screen.getByRole("alert").textContent).toBe("操作未完成，请重试。");
    expect(document.body.textContent).not.toMatch(/private-provider|private-user/);
    expect(screen.queryByRole("region", { name: "私有工作区", hidden: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新连接" }));
    await screen.findByRole("heading", { name: "设计员工甲的任务" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("会话变更通知立即清除草稿，跨标签通知只处理约定键", async () => {
    const recheck = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockResolvedValueOnce(employee()).mockReturnValueOnce(recheck.promise);
    renderWorkspace();
    fireEvent.change(await screen.findByRole("textbox", { name: "未保存设计草稿" }), { target: { value: "旧会话草稿" } });
    fireEvent(window, new StorageEvent("storage", { key: "unrelated-setting" }));
    expect(teamApi.me).toHaveBeenCalledTimes(1);
    fireEvent(window, new StorageEvent("storage", { key: "design-session-change", newValue: "synthetic-change" }));
    expect(screen.queryByRole("region", { name: "私有工作区", hidden: true })).toBeNull();
    await act(async () => recheck.resolve(employee()));
    expect((screen.getByRole("textbox", { name: "未保存设计草稿" }) as HTMLTextAreaElement).value).toBe("");
  });

  it("当前标签会话广播仅保存变更标记，并触发重新核验", async () => {
    vi.mocked(teamApi.me).mockResolvedValue(employee());
    renderWorkspace();
    await screen.findByRole("heading", { name: "设计员工甲的任务" });
    act(() => announceSessionChange());
    await waitFor(() => expect(teamApi.me).toHaveBeenCalledTimes(2));
    expect(localStorage.length).toBe(1);
    expect(localStorage.getItem("design-session-change")).toMatch(/^\d+$/);
    expect(JSON.stringify(localStorage)).not.toMatch(/designer-a|context-a|设计员工甲/);
  });

  it("卸载中止身份读取，迟到结果不能重新绑定已退出页面的身份", async () => {
    const initial = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockReturnValue(initial.promise);
    const view = renderWorkspace();
    const signal = vi.mocked(teamApi.me).mock.calls[0][0];
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => initial.resolve(employee()));
    expect(designContextKey()).not.toBe("context-a");
    focusWindow();
    expect(teamApi.me).toHaveBeenCalledTimes(1);
  });

  it.each([{ status: 401, code: "SESSION_EXPIRED" }, { status: 409, code: "AUTH_CONTEXT_CHANGED" }])("业务 $status/$code 立即清理私有显示并重验身份，原待恢复动作保留", async ({ status, code }) => {
    const recheck = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockResolvedValueOnce(employee()).mockReturnValueOnce(recheck.promise);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code, message: "请重新核对身份" } }), { status })));
    const original = '{"id":"original-action","body":"frozen-body"}';
    sessionStorage.setItem("design-pending:synthetic", original);
    renderWorkspace();
    fireEvent.change(await screen.findByRole("textbox", { name: "未保存设计草稿" }), { target: { value: "原身份私有编辑" } });
    await act(async () => { await teamRequest("/design-handoffs/synthetic/decision", jsonBody({ action: "accept" })).catch(() => undefined); });
    expect(teamApi.me).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "私有工作区", hidden: true })).toBeNull();
    expect(screen.queryByDisplayValue("原身份私有编辑")).toBeNull();
    expect(sessionStorage.getItem("design-pending:synthetic")).toBe(original);
    await act(async () => recheck.resolve(employee({ username: "designer-b", display_name: "设计员工乙", auth_context_id: "context-b" })));
    expect(screen.getByRole("heading", { name: "设计员工乙的任务" })).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "未保存设计草稿" }) as HTMLTextAreaElement).value).toBe("");
    expect(sessionStorage.getItem("design-pending:synthetic")).toBe(original);
    sessionStorage.removeItem("design-pending:synthetic");
  });

  it("普通版本冲突不触发重新 me，保留当前私有编辑", async () => {
    vi.mocked(teamApi.me).mockResolvedValue(employee());
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "STATE_CONFLICT", message: "版本已更新" } }), { status: 409 })));
    renderWorkspace();
    fireEvent.change(await screen.findByRole("textbox", { name: "未保存设计草稿" }), { target: { value: "冲突后保留的编辑" } });
    await act(async () => { await teamRequest("/design-handoffs/synthetic/decision", jsonBody({ action: "accept" })).catch(() => undefined); });
    expect(teamApi.me).toHaveBeenCalledTimes(1);
    expect(screen.getByDisplayValue("冲突后保留的编辑")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("StrictMode 重建核验后旧 me 的迟到错误不能覆盖更新身份", async () => {
    const first = deferred<TeamUser>();
    const second = deferred<TeamUser>();
    vi.mocked(teamApi.me).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise).mockResolvedValueOnce(employee({ auth_context_id: "context-latest" }));
    render(<StrictMode><TeamBoundary>{user => <PrivateWorkspace user={user} />}</TeamBoundary></StrictMode>);
    expect(teamApi.me).toHaveBeenCalledTimes(2);
    focusWindow();
    await screen.findByRole("heading", { name: "设计员工甲的任务" });
    await act(async () => first.reject(new ApiError("SESSION_EXPIRED", "旧会话错误", 401)));
    await act(async () => second.resolve(employee({ display_name: "过时身份" })));
    expect(screen.getByRole("heading", { name: "设计员工甲的任务" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(designContextKey()).toBe("context-latest");
  });
});
