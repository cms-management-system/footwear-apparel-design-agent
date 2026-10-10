import { createElement } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HandoffsPage from "@/app/handoffs/page";
import { bindDesignContext, type TeamUser } from "@/lib/team-api";
import { canReplay, executeAction, freezeAction, loadPending, pendingKey, savePending, workflowApi, type FrozenAction, type Handoff } from "@/lib/design-workflow";

const fetchMock = vi.fn<typeof fetch>();
const handoffId = "handoff-synthetic-a";
const manager = (overrides: Partial<TeamUser> = {}): TeamUser => ({ username: "manager-a", display_name: "设计负责人甲", role: "manager", integration_mode: "managed", auth_context_id: "context-a", instance_id: "design-test", scope_id: "scope-test", ...overrides });
const handoff = (): Handoff => ({ id: handoffId, package_id: "package-synthetic-a", version: "v1", package: { title: "合成设计要求" }, status: "new", assignee: null, project_id: null, submitted_version_id: null, manager_note: "", created_at: "2026-10-10T07:00:00Z", revision: 3, allowed_actions: ["accept"] });
const pending = (overrides: Partial<FrozenAction> = {}): FrozenAction => ({ id: "action-original-123", path: `/design-handoffs/${handoffId}/decision`, method: "POST", body: '{ "action": "accept", "note": "原始意见", "expected_revision": 3 }', context: "context-a", actor: "manager-a", scope: "scope-test", instance: "design-test", created_at: "2026-10-10T07:00:00Z", ...overrides });
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
function routePage(user: TeamUser, mutate: (init: RequestInit) => Promise<Response>, item = handoff()) {
  fetchMock.mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/design-auth/me") return response(user);
    if (path === "/api/design-auth/staff") return response({ items: [{ username: "designer-a", display_name: "设计员工甲", active: true }] });
    if (path.startsWith("/api/design-handoffs?")) return response({ items: [item], total: 1 });
    if (path === `/api/design-handoffs/${handoffId}`) return response(item);
    if (path === "/api/project/7/design-workspace") return response({ project: { id: 7 }, tasks: [], specs: [], assets: [], versions: [{ id: "version-confirmed-a", status: "confirmed", style_direction: { name: "合成已确认方案" } }] });
    if (path === `/api/design-handoffs/${handoffId}/decision`) return mutate(init ?? {});
    if (path.startsWith("/api/design-operations/")) return response({ id: path.split("/").pop(), status: "succeeded", response: { ok: true } });
    throw new Error(`Unexpected mock route: ${path}`);
  });
}
const writes = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_BASE_PATH", "");
  bindDesignContext("context-a");
  sessionStorage.clear();
  window.history.replaceState(null, "", "/handoffs");
});
afterEach(() => {
  cleanup();
  bindDesignContext(null);
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("冻结动作与恢复读取", () => {
  it("冻结后修改输入对象不改变原体，显式重试使用同一动作 ID 和上下文", async () => {
    const input = { action: "accept", note: "第一次意见", expected_revision: 3 };
    const action = freezeAction(manager(), `/design-handoffs/${handoffId}/decision`, input);
    input.note = "后来的编辑";
    fetchMock.mockRejectedValueOnce(new TypeError("response lost")).mockResolvedValueOnce(response({ ok: true }));
    await expect(executeAction(action, manager())).rejects.toMatchObject({ code: "NETWORK_UNAVAILABLE" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await executeAction(action, manager());
    const [first, second] = fetchMock.mock.calls.map(([, init]) => init);
    expect(first?.body).toBe('{"action":"accept","note":"第一次意见","expected_revision":3}');
    expect(second?.body).toBe(first?.body);
    for (const init of [first, second]) {
      const headers = new Headers(init?.headers);
      expect(headers.get("X-Design-Action-Id")).toBe(action.id);
      expect(headers.get("X-Design-Context")).toBe(action.context);
    }
  });

  it("恢复存储的原始字节，不解析再序列化请求正文", async () => {
    const original = pending();
    savePending(manager(), handoffId, original);
    const restored = loadPending(manager(), handoffId);
    expect(restored).toEqual(original);
    fetchMock.mockResolvedValue(response({ ok: true }));
    await executeAction(restored!, manager());
    expect(fetchMock.mock.calls[0][1]?.body).toBe(original.body);
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("X-Design-Action-Id")).toBe(original.id);
  });

  it.each([
    { auth_context_id: "context-b" },
    { username: "manager-b" },
    { scope_id: "scope-other" },
    { instance_id: "design-other" },
  ])("主体或上下文变化只允许查询，不发送旧写动作：%j", async change => {
    const action = pending();
    const current = manager(change);
    expect(canReplay(action, current)).toBe(false);
    await expect(executeAction(action, current)).rejects.toMatchObject({ code: "AUTH_CONTEXT_CHANGED" });
    expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValue(response({ id: action.id, status: "succeeded" }));
    await workflowApi.operation(action.id);
    expect(fetchMock).toHaveBeenCalledWith(`/api/design-operations/${action.id}`, expect.objectContaining({ credentials: "same-origin" }));
    expect(writes()).toHaveLength(0);
  });

  it("同主体新会话可以读回旧动作供查询，但不能重发", () => {
    const old = pending();
    savePending(manager(), handoffId, old);
    const current = manager({ auth_context_id: "context-new" });
    expect(loadPending(current, handoffId)).toEqual(old);
    expect(canReplay(old, current)).toBe(false);
  });

  it.each([
    { username: "manager-b" },
    { scope_id: "scope-other" },
    { instance_id: "design-other" },
  ])("不同人员或工作区不读取旧动作存储：%j", change => {
    savePending(manager(), handoffId, pending());
    expect(loadPending(manager(change), handoffId)).toBeNull();
  });

  it("存储禁用时保留内存冻结动作且不触发网络补发", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("blocked", "SecurityError"); });
    const original = pending();
    expect(() => savePending(manager(), handoffId, original)).not.toThrow();
    expect(original.body).toContain("原始意见");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("恢复存储只接受当前任务已知动作路径", () => {
  it.each(["decision", "submit", "review", "delivery"])("接受当前任务的 %s 动作", endpoint => {
    const original = pending({ path: `/design-handoffs/${handoffId}/${endpoint}` });
    savePending(manager(), handoffId, original);
    expect(loadPending(manager(), handoffId)).toEqual(original);
  });

  it.each([
    `/design-handoffs/other-handoff/decision`,
    `/design-handoffs/${handoffId}/../other-handoff/decision`,
    `/design-handoffs/${handoffId}/%2e%2e/other-handoff/decision`,
    `/design-handoffs/${handoffId}/decision?redirect=other`,
    `/design-handoffs/${handoffId}/decision#fragment`,
    `/design-handoffs/${handoffId}/decision/extra`,
    `/design-handoffs/${handoffId}/arbitrary-action`,
    `/design-handoffs/${handoffId}//decision`,
    `https://example.invalid/design-handoffs/${handoffId}/decision`,
  ])("拒绝非精确白名单路径 %s", path => {
    sessionStorage.setItem(pendingKey(manager(), handoffId), JSON.stringify(pending({ path })));
    expect(loadPending(manager(), handoffId)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    { method: "DELETE" }, { method: "GET" }, { id: "bad/id" },
    { actor: "other-manager" }, { scope: "other-scope" }, { instance: "other-instance" },
  ])("拒绝不匹配的恢复记录：%j", override => {
    sessionStorage.setItem(pendingKey(manager(), handoffId), JSON.stringify(pending(override)));
    expect(loadPending(manager(), handoffId)).toBeNull();
  });
});

describe("交接页面的未知结果恢复", () => {
  it("回执丢失后保留编辑；明确点击重试仍发原体原 ID，读刷新不补发", async () => {
    let attempts = 0;
    routePage(manager(), async () => { if (++attempts === 1) throw new TypeError("response lost"); return response({ ok: true }); });
    render(createElement(HandoffsPage));
    const input = await screen.findByRole("textbox", { name: "处理意见" });
    fireEvent.change(input, { target: { value: "冻结的原始意见" } });
    fireEvent.click(screen.getByRole("button", { name: "确认可执行，接收需求" }));
    await screen.findByRole("alert");
    expect(writes()).toHaveLength(1);
    expect((screen.getByRole("textbox", { name: "处理意见" }) as HTMLTextAreaElement).value).toBe("冻结的原始意见");
    expect((screen.getByRole("textbox", { name: "处理意见" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.getByText(/编辑已暂存并锁定；先查询原记录，再恢复编辑/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "刷新任务" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "刷新任务" }) as HTMLButtonElement).disabled).toBe(false));
    expect(writes()).toHaveLength(1);
    expect((screen.getByRole("textbox", { name: "处理意见" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole("textbox", { name: "处理意见" }) as HTMLTextAreaElement).value).toBe("冻结的原始意见");
    const original = writes()[0][1];
    fireEvent.click(screen.getByRole("button", { name: "原请求重试" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    const retried = writes()[1][1];
    expect(retried?.body).toBe(original?.body);
    expect(String(retried?.body)).toContain("冻结的原始意见");
    expect(new Headers(retried?.headers).get("X-Design-Action-Id")).toBe(new Headers(original?.headers).get("X-Design-Action-Id"));
    await waitFor(() => expect(loadPending(manager(), handoffId)).toBeNull());
  });

  it("新会话恢复旧动作只提供查询，明确结束恢复前不丢掉原动作", async () => {
    const original = pending();
    savePending(manager(), handoffId, original);
    const current = manager({ auth_context_id: "context-new" });
    routePage(current, async () => response({ ok: true }));
    render(createElement(HandoffsPage));
    await screen.findByText("原操作属于之前的会话，当前只能查询。");
    expect((screen.getByRole("button", { name: "原请求重试" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "确认可执行，接收需求" }) as HTMLButtonElement).disabled).toBe(true);
    expect(writes()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "查询原操作" }));
    await screen.findByText("已找到原操作记录。请核对记录与当前任务后结束恢复。");
    expect(writes()).toHaveLength(0);
    expect(loadPending(current, handoffId)).toEqual(original);
    fireEvent.click(screen.getByRole("button", { name: "已核对记录，结束恢复" }));
    await waitFor(() => expect(loadPending(current, handoffId)).toBeNull());
    expect(writes()).toHaveLength(0);
  });

  it("明确拒绝后由用户结束恢复，保留原意见并重新开放编辑", async () => {
    routePage(manager(), async () => response({ error: { code: "REVISION_CONFLICT", message: "任务已有新修订" } }, 409));
    render(createElement(HandoffsPage));
    fireEvent.change(await screen.findByRole("textbox", { name: "处理意见" }), { target: { value: "拒绝后仍要保留的意见" } });
    fireEvent.click(screen.getByRole("button", { name: "确认可执行，接收需求" }));
    const release = await screen.findByRole("button", { name: "原请求已被拒绝，保留编辑并重新核对" });
    expect((screen.getByRole("textbox", { name: "处理意见" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect(loadPending(manager(), handoffId)).not.toBeNull();
    fireEvent.click(release);
    await waitFor(() => expect((screen.getByRole("button", { name: "刷新任务" }) as HTMLButtonElement).disabled).toBe(false));
    const input = screen.getByRole("textbox", { name: "处理意见" }) as HTMLTextAreaElement;
    expect(input.disabled).toBe(false);
    expect(input.value).toBe("拒绝后仍要保留的意见");
    expect(loadPending(manager(), handoffId)).toBeNull();
    fireEvent.change(input, { target: { value: "重新核对后的意见" } });
    expect(input.value).toBe("重新核对后的意见");
    expect(writes()).toHaveLength(1);
  });

  it("待核对原操作时锁定管理者意见、分派字段和回传事件", async () => {
    savePending(manager(), handoffId, pending());
    routePage(manager(), async () => response({ ok: true }), { ...handoff(), status: "accepted", allowed_actions: ["assign", "send"], deliveries: [{ event_id: "event-a", kind: "accepted", status: "prepared" }] });
    render(createElement(HandoffsPage));
    await screen.findByRole("textbox", { name: "处理意见" });
    for (const label of ["处理意见", "设计人员", "本次优先级", "约定日期", "选择待核对事件"]) {
      const control = screen.getByLabelText(label) as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
      expect(control.disabled).toBe(true);
    }
    expect((screen.getByRole("button", { name: "确认分派" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "发送所选交接记录" }) as HTMLButtonElement).disabled).toBe(true);
    expect(writes()).toHaveLength(0);
  });

  it("待核对原操作时锁定员工方案版本，不能用新版本替换冻结提交", async () => {
    const designer = manager({ username: "designer-a", display_name: "设计员工甲", role: "designer" });
    savePending(designer, handoffId, pending({ actor: "designer-a", path: `/design-handoffs/${handoffId}/submit`, body: '{"version_id":"version-confirmed-a","expected_revision":3}' }));
    routePage(designer, async () => response({ ok: true }), { ...handoff(), status: "assigned", project_id: 7, assignee: "designer-a", allowed_actions: ["submit"] });
    render(createElement(HandoffsPage));
    await screen.findByRole("option", { name: "合成已确认方案 · version-confirmed-a" });
    expect((screen.getByRole("combobox", { name: "选择已确认的方案版本" }) as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "提交此版本审查" }) as HTMLButtonElement).disabled).toBe(true);
    expect(writes()).toHaveLength(0);
    expect(loadPending(designer, handoffId)?.body).toBe('{"version_id":"version-confirmed-a","expected_revision":3}');
  });
});
