import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { agentApi, blankSpec, type Workspace } from "@/lib/agent-api";
import { bindDesignContext, type TeamUser } from "@/lib/team-api";
import { engineRequest, pendingEngineAction, queryEngineAction, rememberWorkspace, retryEngineAction } from "@/lib/engine-api";
const fetchMock = vi.fn<typeof fetch>();
let index = 0;
let user: TeamUser;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const workspace = (revision = 4): Workspace => ({ revision, project: { id: 7, name: "合成设计" }, head: { spec_id: "spec-7" }, assets: [], specs: [{ id: "spec-7", status: "draft", spec: blankSpec(), fingerprint: "test" }], tasks: [], versions: [], prompts: [] });
function success(init?: RequestInit, values: Record<string, unknown> = {}) {
  const headers = new Headers(init?.headers); const base = Number(headers.get("X-Design-Revision"));
  return response({ id: "spec-new", action_id: headers.get("X-Design-Action-Id"), base_revision: base, revision: base + 1, ...values });
}
beforeEach(() => {
  index += 1; user = { username: `designer-${index}`, display_name: "合成设计员", role: "designer", integration_mode: "managed", auth_context_id: `context-${index}`, instance_id: "design-test", scope_id: "synthetic" };
  bindDesignContext(user.auth_context_id!, user); sessionStorage.clear(); fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); rememberWorkspace(workspace());
});
afterEach(() => { bindDesignContext(null); vi.unstubAllGlobals(); });
it("单对象写从工作区映射项目，连续成功采用新修订，不降到旧SSE版本", async () => {
  fetchMock.mockImplementation(async (_url, init) => success(init));
  await agentApi.confirmSpec("spec-7");
  rememberWorkspace(workspace(3));
  await agentApi.save(7, blankSpec(), "spec-7");
  expect(fetchMock.mock.calls.map(([, init]) => new Headers(init?.headers).get("X-Design-Revision"))).toEqual(["4", "5"]);
  expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("X-Design-Context")).toBe(user.auth_context_id);
});
it("未读取项目修订时不发请求，避免猜测版本", async () => {
  await expect(agentApi.confirmSpec("missing-spec")).rejects.toMatchObject({ code: "REVISION_REQUIRED" });
  expect(fetchMock).not.toHaveBeenCalled();
});
it("未知结果后原体重试保持动作、路径query和原修订，新的正文不能覆盖", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("lost")).mockImplementation(async (_url, init) => success(init));
  await expect(engineRequest("/project/7/design-specs?mode=exact", { method: "POST", body: '{ "intent": "合成原稿" }' })).rejects.toMatchObject({ code: "NETWORK_UNAVAILABLE" });
  const pending = pendingEngineAction(7)!;
  rememberWorkspace(workspace(9));
  await expect(engineRequest("/project/7/design-specs?mode=exact", { method: "POST", body: '{"intent":"新编辑"}' })).rejects.toMatchObject({ code: "RECOVERY_REQUIRED" });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  await retryEngineAction(7);
  const retry = fetchMock.mock.calls[1][1];
  expect(retry?.body).toBe('{ "intent": "合成原稿" }');
  expect(new Headers(retry?.headers).get("X-Design-Revision")).toBe("4");
  expect(new Headers(retry?.headers).get("X-Design-Action-Id")).toBe(pending.id);
  expect(fetchMock.mock.calls[1][0]).toBe("/api/project/7/design-specs?mode=exact");
  expect(pendingEngineAction(7)).toBeNull();
});
it("换会话只能查询旧操作，查询成功后按新工作区继续", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("lost"));
  await expect(agentApi.save(7, blankSpec(), "spec-7")).rejects.toBeTruthy();
  const old = pendingEngineAction(7)!;
  const newUser = { ...user, auth_context_id: "replacement-context" };
  bindDesignContext(newUser.auth_context_id, newUser); rememberWorkspace(workspace(8));
  await expect(retryEngineAction(7)).rejects.toMatchObject({ code: "AUTH_CONTEXT_CHANGED" });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  fetchMock.mockResolvedValueOnce(response({ action_id: old.id, method: "POST", path: `/api${old.path}`, status_code: 201, result: { action_id: old.id, base_revision: 4, revision: 5 } }));
  await queryEngineAction(7);
  expect(fetchMock.mock.calls[1][1]?.method).toBeUndefined();
  fetchMock.mockImplementation(async (_url, init) => success(init));
  await agentApi.save(7, blankSpec(), "spec-7");
  expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get("X-Design-Revision")).toBe("8");
});
it("404原操作查询不解除未知状态，明确状态冲突保留输入但允许核对后重新操作", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("lost"));
  await expect(agentApi.save(7, blankSpec(), "spec-7")).rejects.toBeTruthy();
  fetchMock.mockResolvedValueOnce(response({ error: { code: "NOT_FOUND", message: "未查到" } }, 404));
  await expect(queryEngineAction(7)).rejects.toMatchObject({ status: 404 });
  expect(pendingEngineAction(7)).not.toBeNull();
  fetchMock.mockResolvedValueOnce(response({ error: { code: "STATE_CONFLICT", message: "已更新" } }, 409));
  await expect(retryEngineAction(7)).rejects.toMatchObject({ code: "STATE_CONFLICT" });
  expect(pendingEngineAction(7)).toBeNull();
});
it("文生图idempotency_key与动作头完全相同；202回执仅排队，无自动第二次提交", async () => {
  fetchMock.mockImplementation(async (_url, init) => success(init, { task_id: "image-task", project_id: 7, prompt_id: "prompt-7", status: "queued" }));
  await agentApi.textToImage(7, "prompt-7");
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const init = fetchMock.mock.calls[0][1];
  expect(JSON.parse(String(init?.body)).idempotency_key).toBe(new Headers(init?.headers).get("X-Design-Action-Id"));
});
it("缺失成功回执字段仍保留原操作，不宣称可恢复完成", async () => {
  fetchMock.mockResolvedValue(response({ id: "spec-unknown" }));
  await expect(agentApi.save(7, blankSpec(), "spec-7")).rejects.toMatchObject({ code: "RESPONSE_UNKNOWN" });
  expect(pendingEngineAction(7)).not.toBeNull();
});
it.each([403, 409])("A迟到%s拒绝只清原A动作，保留B同项目的新unknown记录", async status => {
  let rejectA!: (response: Response) => void;
  fetchMock.mockImplementationOnce(() => new Promise(resolve => { rejectA = resolve; }));
  const a = agentApi.save(7, { ...blankSpec(), intent: "A原稿" }, "spec-7").catch(error => error);
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const aAction = pendingEngineAction(7)!;
  const b = { ...user, username: `${user.username}-B`, auth_context_id: `${user.auth_context_id}-B` };
  bindDesignContext(b.auth_context_id, b); rememberWorkspace(workspace(8));
  fetchMock.mockRejectedValueOnce(new TypeError("B lost acknowledgement"));
  await expect(agentApi.save(7, { ...blankSpec(), intent: "B新稿" }, "spec-7")).rejects.toMatchObject({ code: "NETWORK_UNAVAILABLE" });
  const bAction = pendingEngineAction(7)!; expect(bAction.id).not.toBe(aAction.id);
  rejectA(response({ error: { code: status === 403 ? "ROLE_FORBIDDEN" : "STATE_CONFLICT", message: "A原请求已拒绝" } }, status));
  await a;
  expect(pendingEngineAction(7)?.id).toBe(bAction.id);
  const key = `design-engine-pending:${bAction.owner}:7`; expect(JSON.parse(sessionStorage.getItem(key)!).id).toBe(bAction.id);
  expect(pendingEngineAction(7)?.body).toBe(bAction.body);
});
it("同一主体旧查询迟到成功不能清除后续新的unknown action", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("lost")); await expect(agentApi.save(7, blankSpec(), "spec-7")).rejects.toBeTruthy();
  const original = pendingEngineAction(7)!; const result = { action_id: original.id, method: "POST", path: `/api${original.path}`, status_code: 201, result: { action_id: original.id, base_revision: 4, revision: 5 } };
  let finishOldLookup!: (response: Response) => void;
  fetchMock.mockImplementationOnce(() => new Promise(resolve => { finishOldLookup = resolve; })).mockResolvedValueOnce(response(result));
  const delayed = queryEngineAction(7); await queryEngineAction(7); expect(pendingEngineAction(7)).toBeNull();
  fetchMock.mockRejectedValueOnce(new TypeError("new lost")); await expect(agentApi.save(7, { ...blankSpec(), intent: "后续新稿" }, "spec-7")).rejects.toBeTruthy();
  const newer = pendingEngineAction(7)!;
  finishOldLookup(response(result)); await delayed;
  expect(pendingEngineAction(7)?.id).toBe(newer.id); expect(JSON.parse(sessionStorage.getItem(`design-engine-pending:${newer.owner}:7`)!).id).toBe(newer.id);
});
it("原查询先确认但原写响应仍在途时，不冻结一个尚未发送的新操作", async () => {
  let finish!: (response: Response) => void;
  fetchMock.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const originalWrite = agentApi.save(7, blankSpec(), "spec-7"); await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const original = pendingEngineAction(7)!;
  fetchMock.mockResolvedValueOnce(response({ action_id: original.id, method: "POST", path: `/api${original.path}`, status_code: 201, result: { action_id: original.id, base_revision: 4, revision: 5 } }));
  await queryEngineAction(7);
  await expect(agentApi.save(7, { ...blankSpec(), intent: "新编辑" }, "spec-7")).rejects.toMatchObject({ code: "ACTION_IN_PROGRESS" });
  expect(pendingEngineAction(7)).toBeNull(); expect(fetchMock).toHaveBeenCalledTimes(2);
  finish(response({ action_id: original.id, base_revision: 4, revision: 5 })); await originalWrite; expect(pendingEngineAction(7)).toBeNull();
});
