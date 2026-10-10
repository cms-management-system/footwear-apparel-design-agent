import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { bindDesignContext, type TeamUser } from "@/lib/team-api";
import { executeDirect, freezeDirect, loadDirect, queryDirect, type DirectInput, type DirectResult } from "@/lib/direct-create";
import { executeProjectAction, freezeProjectAction, projectActions, queryProjectAction } from "@/lib/project-metadata";
let index = 0; let user: TeamUser;
const input: DirectInput = { entry_mode: "idea", text: " 设计通勤鞋\n保留圆头 ", output_kind: "effect_image", intent: "generate_image", authorized: true };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const receipt = (id: string): DirectResult => ({ project_id: 91, source_mode: "independent", action_id: id, base_revision: 0, revision: 1, message_id: "m", intent_id: "i", run_id: "r", run_status: "queued", url: "/projects/91" });
beforeEach(() => { sessionStorage.clear(); user = { username: `direct-owner-${++index}`, display_name: "合成", role: "designer", integration_mode: "managed", instance_id: "fixture-direct", scope_id: "scope", auth_context_id: `ctx-${index}` }; bindDesignContext(user.auth_context_id!, user); });
afterEach(() => { vi.unstubAllGlobals(); bindDesignContext(null); });
it("direct一体原文/意图只有一次POST，无业务revision，201原回执退休", async () => {
  const a = freezeDirect(user, input); const f = vi.fn().mockResolvedValue(response(receipt(a.id), 201)); vi.stubGlobal("fetch", f);
  expect(await executeDirect(a, user)).toMatchObject({ project_id: 91, revision: 1 });
  expect(f).toHaveBeenCalledTimes(1); expect(f.mock.calls[0][0]).toBe("/api/design-projects/direct"); expect(JSON.parse(f.mock.calls[0][1].body)).toEqual(input);
  expect(new Headers(f.mock.calls[0][1].headers).has("X-Design-Revision")).toBe(false); expect(loadDirect(user)).toBeNull();
});
it("503未知保原动作，阻新建；换context本人查询原201不重发且校验原revision", async () => {
  const a = freezeDirect(user, input); const f = vi.fn().mockResolvedValueOnce(response({ error: { code: "SERVICE_UNAVAILABLE", message: "暂不可用" } }, 503)); vi.stubGlobal("fetch", f);
  await expect(executeDirect(a, user)).rejects.toMatchObject({ status: 503 }); expect(loadDirect(user)?.id).toBe(a.id); expect(() => freezeDirect(user, input)).toThrow();
  const next = { ...user, auth_context_id: "new-context" }; bindDesignContext(next.auth_context_id, next);
  await expect(executeDirect(a, next)).rejects.toMatchObject({ code: "AUTH_CONTEXT_CHANGED" }); expect(f).toHaveBeenCalledTimes(1);
  f.mockResolvedValueOnce(response({ action_id: a.id, method: "POST", path: "/api/design-projects/direct", status_code: 201, result: { ...receipt(a.id), revision: 3 } }));
  await expect(queryDirect(a, next)).rejects.toMatchObject({ code: "RESPONSE_UNKNOWN" }); expect(loadDirect(next)?.id).toBe(a.id);
  f.mockResolvedValueOnce(response({ action_id: a.id, method: "POST", path: "/api/design-projects/direct", status_code: 201, result: receipt(a.id) })); await queryDirect(a, next);
  expect(f.mock.calls.slice(1).every(c => c[1].method === undefined)).toBe(true); expect(loadDirect(next)).toBeNull();
});
it("迟到403不能清另一账号的新unknown创建", async () => {
  const a = freezeDirect(user, input); const originalUser = user;
  let finish!: (v: Response) => void; const f = vi.fn().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValueOnce(response({}, 503)); vi.stubGlobal("fetch", f);
  const old = executeDirect(a, user); const other = { ...user, username: `${user.username}-other` }; bindDesignContext(other.auth_context_id!, other);
  const b = freezeDirect(other, input); await expect(executeDirect(b, other)).rejects.toMatchObject({ status: 503 });
  finish(response({ error: { code: "ROLE_FORBIDDEN", message: "禁止" } }, 403)); await expect(old).rejects.toMatchObject({ status: 403 }); expect(loadDirect(other)?.id).toBe(b.id); expect(loadDirect(originalUser)).toBeNull();
});
it("空白动作关联均null，超长原文不冻结，不以已完成run替换创建queued回执", async () => {
  expect(() => freezeDirect(user, { ...input, text: "字".repeat(4001) })).toThrow(); expect(loadDirect(user)).toBeNull();
  const a = freezeDirect(user, { entry_mode: "blank", text: "", intent: "none", authorized: false, output_kind: "effect_image" });
  const f = vi.fn().mockResolvedValue(response({ ...receipt(a.id), message_id: null, intent_id: null, run_id: null, run_status: null }, 201)); vi.stubGlobal("fetch", f);
  await executeDirect(a, user); expect(JSON.parse(f.mock.calls[0][1].body).authorized).toBe(false);
});
it("rename/archive严格business修订；归档丢回执在列表缺卡时仍可查最小回执", async () => {
  const a = freezeProjectAction(91, 6, user, "新名字"); const f = vi.fn().mockResolvedValueOnce(response({ project_id: 91, title: "新名字", archived: false, action_id: a.id, base_revision: 6, revision: 7 })); vi.stubGlobal("fetch", f);
  await executeProjectAction(a, user); expect(f.mock.calls[0][1].method).toBe("PATCH"); expect(new Headers(f.mock.calls[0][1].headers).get("X-Design-Revision")).toBe("6"); expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ expected_revision: 6, title: "新名字" });
  const b = freezeProjectAction(91, 7, user); f.mockResolvedValueOnce(response({}, 503)); await expect(executeProjectAction(b, user)).rejects.toMatchObject({ status: 503 }); expect(projectActions(user)).toHaveLength(1);
  f.mockResolvedValueOnce(response({ action_id: b.id, method: "POST", path: "/api/design-projects/91/archive", status_code: 200, result: { project_id: 91, archived: true, action_id: b.id, base_revision: 7, revision: 8 } })); await queryProjectAction(b, user); expect(projectActions(user)).toHaveLength(0); expect(f.mock.calls[2][1].method).toBeUndefined();
});
it("metadata409保原名称且原动作退休，无自动重复；他人恢复不可读取", async () => {
  const a = freezeProjectAction(91, 7, user); const f = vi.fn().mockResolvedValue(response({ error: { code: "PROJECT_BUSY", message: "原任务待核对" } }, 409)); vi.stubGlobal("fetch", f);
  await expect(executeProjectAction(a, user)).rejects.toMatchObject({ code: "PROJECT_BUSY" }); expect(projectActions(user)).toHaveLength(0); expect(f).toHaveBeenCalledTimes(1);
  expect(projectActions({ ...user, username: "other" })).toHaveLength(0);
});
