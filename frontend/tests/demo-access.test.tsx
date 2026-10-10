import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bindDesignContext, currentDesignUser, jsonBody, storageOwner, teamApi, teamRequest, type TeamUser } from "@/lib/team-api";
import { demoDestination, navigateDemo, navigateDemoEntry, navigateTeamWorkspace, resolveDemoRole } from "@/lib/demo-access";
import DemoEntry from "@/components/team/DemoEntry";
import AuthEntryGate from "@/components/team/AuthEntryGate";
import { TeamBoundary } from "@/components/team/TeamSession";
import SettingsPage from "@/app/settings/page";
import { directKey, freezeDirect, loadDirect } from "@/lib/direct-create";
import { canvasStorageKey } from "@/lib/workspace-api";
import { executionDraftKey, readExecutionDraft, storeExecutionDraft } from "@/lib/workspace-drafts";
import { canReplay, freezeAction, pendingKey } from "@/lib/design-workflow";
import { createPendingKey } from "@/lib/design-projects";

vi.mock("@/lib/demo-access", async original => ({ ...await original<typeof import("@/lib/demo-access")>(), navigateDemo: vi.fn(), navigateDemoEntry: vi.fn(), navigateTeamWorkspace: vi.fn() }));
const fetchMock = vi.fn<typeof fetch>();
const demo = (changes: Partial<TeamUser> = {}): TeamUser => ({ username: "design-employee-a", display_name: "演示设计员工", role: "designer", integration_mode: "managed", instance_id: "demo-instance", scope_id: "demo-scope", auth_context_id: "demo-context-a", access_mode: "demo", demo_entry_urls: { designer: "/demo/designer", manager: "/demo/manager" }, ...changes });
const manager = () => demo({ username: "design-manager", display_name: "演示设计经理", role: "manager", auth_context_id: "demo-context-m" });
const ordinary = (): TeamUser => demo({ access_mode: "authenticated", demo_entry_urls: null });
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function fail(code: string, status: number) { return response({ error: { code, message: "服务器拒绝该入口" } }, status); }
function deferred<T>() { let resolve!: (x: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { resolve, promise }; }
beforeEach(() => { bindDesignContext(null); fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); vi.mocked(navigateDemo).mockClear(); vi.mocked(navigateDemoEntry).mockClear(); vi.mocked(navigateTeamWorkspace).mockClear(); sessionStorage.clear(); localStorage.clear(); window.history.replaceState(null, "", "/"); });
afterEach(() => { cleanup(); bindDesignContext(null); vi.unstubAllGlobals(); vi.restoreAllMocks(); window.history.replaceState(null, "", "/"); });

describe("服务端演示身份与入口", () => {
  it.each(["designer", "manager"] as const)("%s 直达自动核验模式、bootstrap、回读身份，不带业务字段或原context", async role => {
    const selected = role === "designer" ? demo() : manager();
    fetchMock.mockResolvedValueOnce(response(demo())).mockResolvedValueOnce(response(selected)).mockResolvedValueOnce(response(selected));
    render(<DemoEntry entryRole={role} />);
    expect(screen.queryByRole("textbox")).toBeNull();
    await waitFor(() => expect(navigateDemo).toHaveBeenCalledWith(role));
    expect(fetchMock.mock.calls.map(([p]) => p)).toEqual(["/api/design-auth/me", "/api/design-auth/demo-access", "/api/design-auth/me"]);
    const init = fetchMock.mock.calls[1][1];
    expect(JSON.parse(String(init?.body))).toEqual({ role });
    expect(new Headers(init?.headers).has("X-Design-Context")).toBe(false);
    expect(new Headers(init?.headers).has("X-Design-Action-Id")).toBe(false);
    expect(localStorage.getItem("design-session-change")).toMatch(/^\d+$/);
    expect(demoDestination(role)).toBe(role === "designer" ? "/?demo_role=designer" : "/handoffs?demo_role=manager");
  });
  it("普通认证不会调用bootstrap或把角色参数当权限", async () => {
    fetchMock.mockResolvedValue(response(ordinary()));
    await expect(resolveDemoRole("manager")).rejects.toMatchObject({ code: "DEMO_ACCESS_DISABLED" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("关闭开关的404原样显示，不自动跳登录循环或重发", async () => {
    fetchMock.mockResolvedValueOnce(response(demo())).mockResolvedValueOnce(fail("DEMO_ACCESS_DISABLED", 404));
    render(<DemoEntry entryRole="manager" />);
    await screen.findByRole("alert"); expect(fetchMock).toHaveBeenCalledTimes(2); expect(navigateDemo).not.toHaveBeenCalled();
  });
  it("bootstrap网络未知仅回读me，不能重发；身份不符不进入经理", async () => {
    fetchMock.mockResolvedValueOnce(response(demo())).mockRejectedValueOnce(new TypeError("lost receipt")).mockResolvedValueOnce(response(demo()));
    await expect(resolveDemoRole("manager")).rejects.toMatchObject({ code: "DEMO_ROLE_UNCONFIRMED" });
    expect(fetchMock.mock.calls.filter(([p]) => String(p).endsWith("demo-access"))).toHaveLength(1);
  });
  it("bootstrap回执丢失后读取已生效角色即可恢复，零业务请求", async () => {
    fetchMock.mockResolvedValueOnce(response(demo())).mockRejectedValueOnce(new TypeError("lost receipt")).mockResolvedValueOnce(response(manager()));
    await expect(resolveDemoRole("manager")).resolves.toEqual(manager()); expect(fetchMock).toHaveBeenCalledTimes(3);
  });
  it("bootstrap非JSON成功回执仍仅回读身份，不重复选择角色", async () => {
    fetchMock.mockResolvedValueOnce(response(demo())).mockResolvedValueOnce(new Response("bad response", { status: 200 })).mockResolvedValueOnce(response(manager()));
    await expect(resolveDemoRole("manager")).resolves.toEqual(manager()); expect(fetchMock).toHaveBeenCalledTimes(3);
  });
  it.each([
    { access_mode: "superuser" }, { demo_entry_urls: null }, { demo_entry_urls: { designer: "https://untrusted.example", manager: "/demo/manager" } }, { auth_context_id: "" }, { access_mode: "authenticated", demo_entry_urls: { designer: "/demo/designer", manager: "/demo/manager" } },
  ])("身份字段异常不得进入私有区：%j", async change => {
    fetchMock.mockResolvedValue(response({ ...demo(), ...change }));
    await expect(teamApi.me()).rejects.toMatchObject({ code: "IDENTITY_UNKNOWN" });
  });
  it("初次bootstrap豁免context，其他写仍拒绝", async () => {
    fetchMock.mockResolvedValue(response(demo()));
    await teamApi.demoAccess("designer");
    await expect(teamRequest("/design-projects/direct", jsonBody({ entry_mode: "blank" }))).rejects.toMatchObject({ code: "CONTEXT_REQUIRED" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("旧认证入口与设置", () => {
  it.each(["designer", "manager"] as const)("demo旧登录/注册入口直接转%s，账号表单不挂载", async role => {
    fetchMock.mockResolvedValue(response(role === "designer" ? demo() : manager()));
    render(<AuthEntryGate><input aria-label="账号" /></AuthEntryGate>);
    await waitFor(() => expect(navigateDemoEntry).toHaveBeenCalledWith(role)); expect(screen.queryByLabelText("账号")).toBeNull();
  });
  it("demo登录书签可选择合法角色，正常认证仍展示原内容", async () => {
    window.history.replaceState(null, "", "/login?demo_role=manager"); fetchMock.mockResolvedValue(response(demo()));
    const view = render(<AuthEntryGate><input aria-label="账号" /></AuthEntryGate>);
    await waitFor(() => expect(navigateDemoEntry).toHaveBeenCalledWith("manager")); view.unmount();
    fetchMock.mockResolvedValue(fail("SESSION_EXPIRED", 401));
    render(<AuthEntryGate><input aria-label="账号" /></AuthEntryGate>); await screen.findByLabelText("账号");
  });
  it("身份服务503不会显示账号表单误导用户", async () => {
    fetchMock.mockResolvedValue(fail("CONFIGURATION_ERROR", 503)); render(<AuthEntryGate><input aria-label="账号" /></AuthEntryGate>);
    await screen.findByRole("alert"); expect(screen.queryByLabelText("账号")).toBeNull(); expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("demo设置不提供密码、人员写入或退出死路", async () => {
    fetchMock.mockResolvedValue(response(demo())); render(<SettingsPage />);
    await screen.findByRole("heading", { name: "演示身份与团队" });
    expect(screen.queryByLabelText("当前密码")).toBeNull(); expect(screen.queryByRole("button", { name: "退出登录" })).toBeNull();
    expect(screen.getByRole("link", { name: "返回演示入口" }).getAttribute("href")).toBe("/demo/designer");
    expect(screen.getAllByRole("link", { name: "切换为设计经理" }).every(link => link.getAttribute("href") === "/demo/manager")).toBe(true);
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });
});

describe("角色上下文隔离及取消", () => {
  it("所有demo草稿和pending键隔离角色/context；原认证键保持且跨context可恢复", () => {
    const a = demo(), m = demo({ role: "manager" }), next = demo({ auth_context_id: "demo-new-context" });
    const keys = [(u: TeamUser) => storageOwner(u), (u: TeamUser) => directKey(u), (u: TeamUser) => canvasStorageKey(77, u), (u: TeamUser) => executionDraftKey(u, 77), (u: TeamUser) => pendingKey(u, "demo-handoff"), (u: TeamUser) => createPendingKey(u)];
    for (const key of keys) { expect(key(a)).not.toBe(key(m)); expect(key(a)).not.toBe(key(next)); expect(key(ordinary())).toBe(key({ ...ordinary(), auth_context_id: "other" })); }
    storeExecutionDraft(a, 77, { text: "员工草稿", avoid: "", kind: "effect_image", base: "", region: "", expectedPrompt: null, expectedSpec: null, expectedLatest: null });
    expect(readExecutionDraft(m, 77)).toBeNull(); expect(readExecutionDraft(next, 77)).toBeNull(); expect(readExecutionDraft(a, 77)?.text).toBe("员工草稿");
    const action = freezeDirect(a, { entry_mode: "blank", text: "", output_kind: "effect_image", intent: "none", authorized: false });
    expect(loadDirect(m)).toBeNull(); expect(loadDirect(next)).toBeNull(); expect(loadDirect(a)?.id).toBe(action.id); expect(canReplay(action, next)).toBe(false);
    expect(canReplay(freezeAction(a, "/design-handoffs/demo/review", {}), next)).toBe(false);
  });
  it("切换身份取消旧业务请求，迟到旧success不能作为新身份结果", async () => {
    const old = deferred<Response>(); fetchMock.mockReturnValue(old.promise);
    bindDesignContext(demo().auth_context_id!, demo());
    const result = teamRequest("/design-workspace/77").catch(error => error);
    const signal = fetchMock.mock.calls[0][1]?.signal;
    bindDesignContext(null); expect(signal?.aborted).toBe(true);
    bindDesignContext(manager().auth_context_id!, manager()); old.resolve(response({ old_private_workspace: true }));
    expect(await result).toMatchObject({ name: "AbortError" }); expect(currentDesignUser()?.role).toBe("manager");
  });
  it("跨标签切换立即隐藏原投影，旧查询参数消费后不会把经理切回员工", async () => {
    window.history.replaceState(null, "", "/handoffs?demo_role=designer");
    fetchMock.mockResolvedValueOnce(response(demo()));
    render(<TeamBoundary>{user => <h1>{user.display_name}私有任务</h1>}</TeamBoundary>);
    await screen.findByRole("heading", { name: "演示设计员工私有任务" }); expect(window.location.search).toBe("");
    const recheck = deferred<Response>(); fetchMock.mockReturnValueOnce(recheck.promise);
    act(() => fireEvent(window, new StorageEvent("storage", { key: "design-session-change", newValue: "new" })));
    expect(screen.queryByRole("heading", { name: "演示设计员工私有任务" })).toBeNull();
    await act(async () => recheck.resolve(response(manager())));
    await screen.findByRole("heading", { name: "演示设计经理私有任务" });
    expect(fetchMock.mock.calls.some(([p]) => String(p).endsWith("demo-access"))).toBe(false);
  });
  it("默认首页恢复经理工作台，不创建业务对象", async () => {
    fetchMock.mockResolvedValue(response(manager())); render(<TeamBoundary>{user => <h1>{user.display_name}</h1>}</TeamBoundary>);
    await waitFor(() => expect(navigateTeamWorkspace).toHaveBeenCalledWith("manager")); expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("入口卸载取消me，迟到结果不得bootstrap或跳转", async () => {
    const old = deferred<Response>(); fetchMock.mockReturnValue(old.promise); const view = render(<DemoEntry entryRole="manager" />);
    const signal = fetchMock.mock.calls[0][1]?.signal; view.unmount(); expect(signal?.aborted).toBe(true);
    await act(async () => old.resolve(response(demo()))); expect(fetchMock).toHaveBeenCalledTimes(1); expect(navigateDemo).not.toHaveBeenCalled();
  });
});
