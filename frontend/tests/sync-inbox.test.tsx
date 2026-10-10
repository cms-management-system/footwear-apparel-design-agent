import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SyncInbox from "@/components/team/SyncInbox";
import { bindDesignContext, type TeamUser } from "@/lib/team-api";
import { freezeAction, pendingKey } from "@/lib/design-workflow";

const user: TeamUser = { username: "synthetic-manager", display_name: "合成负责人", role: "manager", integration_mode: "managed", auth_context_id: "context-test", scope_id: "scope-test", instance_id: "design-test" };
const fetchMock = vi.fn<typeof fetch>();
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); bindDesignContext(user.auth_context_id!); sessionStorage.clear(); });
afterEach(() => { cleanup(); bindDesignContext(null); sessionStorage.clear(); vi.unstubAllGlobals(); });

it("页面读取不自动同步，每一页均由管理者明确触发", async () => {
  fetchMock.mockResolvedValueOnce(response({ next_cursor: "cursor-2" })).mockResolvedValueOnce(response({ next_cursor: null }));
  const changed = vi.fn();
  render(<SyncInbox user={user} onChanged={changed} />);
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "同步产品已批准需求" }));
  const next = await screen.findByRole("button", { name: "继续同步下一页" });
  await waitFor(() => expect((next as HTMLButtonElement).disabled).toBe(false));
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(fetchMock.mock.calls[0][1]?.body).toBe('{"limit":20}');
  fireEvent.click(next);
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(2));
  expect(fetchMock.mock.calls[1][1]?.body).toBe('{"limit":20,"cursor":"cursor-2"}');
  expect(screen.queryByRole("button", { name: "继续同步下一页" })).toBeNull();
});

it("响应未知后保留原字节和动作身份，重新挂载不自动重发", async () => {
  fetchMock.mockRejectedValueOnce(new TypeError("lost"));
  const view = render(<SyncInbox user={user} onChanged={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "同步产品已批准需求" }));
  await screen.findByRole("alert");
  const original = fetchMock.mock.calls[0][1];
  view.unmount();
  fetchMock.mockResolvedValue(response({ next_cursor: null }));
  render(<SyncInbox user={user} onChanged={vi.fn()} />);
  expect(fetchMock).toHaveBeenCalledTimes(1);
  fireEvent.click(await screen.findByRole("button", { name: "重试原同步" }));
  await screen.findByText("本页已核验并记录。技术接收不会自动接单或分派。");
  const retry = fetchMock.mock.calls[1][1];
  expect(retry?.body).toBe(original?.body);
  expect(new Headers(retry?.headers).get("X-Design-Action-Id")).toBe(new Headers(original?.headers).get("X-Design-Action-Id"));
});

it("新会话只查原记录，失败快照不显示为同步成功", async () => {
  const action = freezeAction(user, "/design-handoffs/sync", { limit: 20 });
  sessionStorage.setItem(pendingKey(user, "_inbox_sync"), JSON.stringify(action));
  const nextUser = { ...user, auth_context_id: "context-new" };
  bindDesignContext(nextUser.auth_context_id);
  fetchMock.mockResolvedValue(response({ status_code: 409, result: { error: { code: "STATE_CONFLICT" } } }));
  const changed = vi.fn();
  render(<SyncInbox user={nextUser} onChanged={changed} />);
  expect((await screen.findByRole("button", { name: "重试原同步" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "查询原同步" }));
  await screen.findByText("原同步记录尚未确认成功。请求仍保留，请核对后再恢复。");
  expect(changed).not.toHaveBeenCalled();
  expect(fetchMock.mock.calls[0][1]?.method).toBeUndefined();
  expect(sessionStorage.getItem(pendingKey(user, "_inbox_sync"))).not.toBeNull();
});
