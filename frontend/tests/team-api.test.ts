import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, bindDesignContext, DESIGN_SESSION_INVALIDATED_EVENT, errorText, isAbort, jsonBody, teamApi, teamRequest } from "@/lib/team-api";

const fetchMock = vi.fn<typeof fetch>();
const identity = () => ({ username: "designer-a", display_name: "设计员工甲", role: "designer", integration_mode: "managed", auth_context_id: "context-a", instance_id: "design-test", scope_id: "scope-test" });
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function abortableFetch() {
  fetchMock.mockImplementation((_input, init) => new Promise<Response>((_resolve, reject) => {
    const signal = init?.signal;
    const abort = () => reject(new DOMException("aborted", "AbortError"));
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  }));
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  bindDesignContext("test-context");
});
afterEach(() => {
  bindDesignContext(null);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("设计请求的异常与恢复边界", () => {
  it("读取使用当前同源会话且不缓存私有结果", async () => {
    vi.stubEnv("NEXT_PUBLIC_BASE_PATH", "/design-team");
    fetchMock.mockResolvedValue(response({ items: [] }));
    await expect(teamRequest("/design-auth/staff")).resolves.toEqual({ items: [] });
    expect(fetchMock).toHaveBeenCalledWith("/design-team/api/design-auth/staff", expect.objectContaining({ credentials: "same-origin", cache: "no-store", signal: expect.any(AbortSignal) }));
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has("X-Design-Action-Id")).toBe(false);
  });

  it("调用方取消读取时传递 AbortError，不转成连接失败或重新请求", async () => {
    abortableFetch();
    const controller = new AbortController();
    const pending = teamApi.me(controller.signal).catch(error => error);
    controller.abort();
    const error = await pending;
    expect(isAbort(error)).toBe(true);
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("已经取消的读取保持取消，不产生成功身份", async () => {
    abortableFetch();
    const controller = new AbortController();
    controller.abort();
    const error = await teamApi.me(controller.signal).catch(error => error);
    expect(isAbort(error)).toBe(true);
  });

  it("读取超时会中止在途请求并提供可恢复错误，不自动重试", async () => {
    vi.useFakeTimers();
    abortableFetch();
    const pending = teamRequest("/design-auth/me", {}, 50).catch(error => error);
    await vi.advanceTimersByTimeAsync(50);
    const error = await pending;
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "RESPONSE_UNKNOWN" });
    expect(isAbort(error)).toBe(false);
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("401 保留正式状态和错误码供会话边界重新登录", async () => {
    fetchMock.mockResolvedValue(response({ error: { code: "SESSION_EXPIRED", message: "请重新登录" } }, 401));
    await expect(teamApi.me()).rejects.toMatchObject({ name: "ApiError", status: 401, code: "SESSION_EXPIRED", message: "请重新登录" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    { error: { code: { secret: "provider-secret" }, message: { trace: "private-trace" } }, detail: [{ input: "private-draft" }] },
    { detail: { trace: "private-trace", input: "private-draft" } },
    [{ secret: "provider-secret" }],
  ])("错误对象不被拼接成用户消息：%j", async body => {
    fetchMock.mockResolvedValue(response(body, 422));
    const error = await teamRequest("/design-auth/me").catch(error => error);
    expect(error).toMatchObject({ code: "HTTP_422", status: 422, message: "填写内容不符合要求，请检查后重试。" });
    expect(errorText(error)).not.toMatch(/private-|provider-secret|\[object Object\]/);
  });

  it("非 JSON 失败响应与任意抛出对象使用安全文案", async () => {
    fetchMock.mockResolvedValueOnce(new Response("<html>private-trace</html>", { status: 503 }));
    await expect(teamRequest("/design-auth/me")).rejects.toMatchObject({ status: 503, message: "这项服务暂不可用，已保存的内容仍然保留。" });
    fetchMock.mockRejectedValueOnce({ trace: "private-trace" });
    await expect(teamRequest("/design-auth/me")).rejects.toMatchObject({ code: "NETWORK_UNAVAILABLE" });
    expect(errorText({ trace: "private-trace" })).toBe("操作未完成，请重试。");
  });

  it("成功状态下的无效正文不会被当作可用记录", async () => {
    fetchMock.mockResolvedValue(response("private-trace"));
    await expect(teamRequest("/design-auth/me")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it.each([409, 429, 500, 503])("写动作收到 %i 后不自动重复发送", async status => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(response({ error: { code: "ACTION_NOT_CONFIRMED", message: "请核对原操作" } }, status));
    await expect(teamRequest("/design-auth/logout", jsonBody({}))).rejects.toMatchObject({ status });
    await vi.advanceTimersByTimeAsync(60000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("写动作断网或超时不自动重发原操作", async () => {
    vi.useFakeTimers();
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await expect(teamRequest("/design-auth/logout", jsonBody({}))).rejects.toMatchObject({ code: "NETWORK_UNAVAILABLE" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockReset();
    abortableFetch();
    const pending = teamRequest("/design-auth/logout", jsonBody({}), 25).catch(error => error);
    await vi.advanceTimersByTimeAsync(60000);
    expect(await pending).toMatchObject({ code: "RESPONSE_UNKNOWN" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("写操作上下文与正式身份", () => {
  it("正在核验身份时阻止写入，但允许重新登录", async () => {
    bindDesignContext(null);
    await expect(teamApi.logout()).rejects.toMatchObject({ code: "CONTEXT_REQUIRED" });
    expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValue(response(identity()));
    await teamApi.login("designer-a", "synthetic-password");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("已验证的写动作携带当前上下文和动作标识", async () => {
    bindDesignContext("verified-context");
    fetchMock.mockResolvedValue(response({ ok: true }));
    await teamApi.logout();
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.get("X-Design-Context")).toBe("verified-context");
    expect(headers.get("X-Design-Action-Id")).toMatch(/^[\da-f-]{36}$/i);
  });

  it("明确核对原动作时保留调用方提供的上下文、动作标识和原体", async () => {
    fetchMock.mockResolvedValue(response({ ok: true }));
    const originalBody = '{"operation":"original-action"}';
    await teamRequest("/design-auth/logout", { method: "POST", body: originalBody, headers: { "X-Design-Context": "original-context", "X-Design-Action-Id": "original-action" } });
    const init = fetchMock.mock.calls[0][1];
    expect(new Headers(init?.headers).get("X-Design-Context")).toBe("original-context");
    expect(new Headers(init?.headers).get("X-Design-Action-Id")).toBe("original-action");
    expect(init?.body).toBe(originalBody);
  });

  it("调用方显式指定上下文时仍为新写动作补齐动作标识", async () => {
    fetchMock.mockResolvedValue(response({ ok: true }));
    await teamRequest("/design-auth/logout", { ...jsonBody({}), headers: { "Content-Type": "application/json", "X-Design-Context": "explicit-context" } });
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.get("X-Design-Context")).toBe("explicit-context");
    expect(headers.get("X-Design-Action-Id")).toMatch(/^[\da-f-]{36}$/i);
  });

  it("managed 身份必须具备已约定的主体、角色与工作区上下文", async () => {
    fetchMock.mockResolvedValue(response(identity()));
    await expect(teamApi.me()).resolves.toEqual(identity());
  });

  it.each(["username", "display_name", "role", "auth_context_id", "instance_id", "scope_id"])("缺少 %s 时拒绝 managed 身份", async field => {
    const incomplete: Record<string, unknown> = identity();
    delete incomplete[field];
    fetchMock.mockResolvedValue(response(incomplete));
    await expect(teamApi.me()).rejects.toMatchObject({ code: "IDENTITY_UNKNOWN" });
  });

  it.each(["username", "display_name", "auth_context_id", "instance_id", "scope_id"])("拒绝 %s 是任意对象的身份，避免渲染崩溃或错误绑定", async field => {
    fetchMock.mockResolvedValue(response({ ...identity(), [field]: { unexpected: "private-payload" } }));
    await expect(teamApi.me()).rejects.toMatchObject({ code: "IDENTITY_UNKNOWN" });
  });
});

describe("业务请求使当前标签身份失效", () => {
  it.each([
    { status: 401, code: "SESSION_EXPIRED" },
    { status: 409, code: "AUTH_CONTEXT_CHANGED" },
  ])("业务 $status/$code 通知重新验身份，不写入身份或清理待恢复动作", async ({ status, code }) => {
    const listener = vi.fn();
    window.addEventListener(DESIGN_SESSION_INVALIDATED_EVENT, listener);
    const storedAction = '{"id":"original-action","body":"original-bytes"}';
    sessionStorage.setItem("design-pending:synthetic", storedAction);
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    try {
      fetchMock.mockResolvedValue(response({ error: { code, message: "请重新核对身份" } }, status));
      await expect(teamRequest("/design-handoffs/synthetic/decision", jsonBody({ action: "accept" }))).rejects.toMatchObject({ code, status });
      expect(listener).toHaveBeenCalledTimes(1);
      const event = listener.mock.calls[0][0] as Event;
      expect(event).toBeInstanceOf(Event);
      expect("detail" in event).toBe(false);
      expect(storageWrite).not.toHaveBeenCalled();
      expect(sessionStorage.getItem("design-pending:synthetic")).toBe(storedAction);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(DESIGN_SESSION_INVALIDATED_EVENT, listener);
      sessionStorage.removeItem("design-pending:synthetic");
    }
  });

  it.each([
    { path: "/design-auth/me", status: 401, code: "SESSION_EXPIRED" },
    { path: "/design-auth/me?check=1", status: 409, code: "AUTH_CONTEXT_CHANGED" },
    { path: "/design-auth/login", status: 401, code: "INVALID_LOGIN" },
    { path: "/design-auth/login", status: 409, code: "AUTH_CONTEXT_CHANGED" },
    { path: "/design-handoffs/synthetic/decision", status: 409, code: "STATE_CONFLICT" },
    { path: "/design-handoffs/synthetic", status: 403, code: "FORBIDDEN" },
  ])("$path 的 $status/$code 不触发身份核验循环或误清草稿", async ({ path, status, code }) => {
    const listener = vi.fn();
    window.addEventListener(DESIGN_SESSION_INVALIDATED_EVENT, listener);
    try {
      fetchMock.mockResolvedValue(response({ error: { code, message: "操作尚未完成" } }, status));
      await expect(teamRequest(path)).rejects.toMatchObject({ status, code });
      expect(listener).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally { window.removeEventListener(DESIGN_SESSION_INVALIDATED_EVENT, listener); }
  });
});
