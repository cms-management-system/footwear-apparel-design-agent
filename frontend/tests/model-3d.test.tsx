import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import Model3DPanel from "@/components/design/Model3DPanel";
import { agentApi, type Capabilities, type Version } from "@/lib/agent-api";

vi.mock("@/lib/agent-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/agent-api")>();
  return { ...original, agentApi: { ...original.agentApi, generate3D: vi.fn() } };
});

const version: Version = { id: "selected-version", status: "ready_for_review", spec_id: "s1", parent_version_id: null, review: null };
const caps = { three_d: { enabled: true, provider: "火山方舟影眸", estimated_cost_fen: 180, monthly_allocation_fen: 180, note: "" } } as Capabilities;
afterEach(() => { cleanup(); sessionStorage.clear(); vi.resetAllMocks(); });

it("只为当前方案提交一次3D任务，保留原效果图", async () => {
  vi.mocked(agentApi.generate3D).mockResolvedValue({ id: "m1", version_id: version.id, status: "queued", estimated_cost_fen: 180 });
  render(<Model3DPanel version={version} title="方案 2 / 3" caps={caps} />);
  expect(screen.getByRole("img", { name: "方案 2 / 3" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /制作这款的 3D 预览/ }));
  await waitFor(() => expect(agentApi.generate3D).toHaveBeenCalledOnce());
  expect(vi.mocked(agentApi.generate3D).mock.calls[0]?.[0]).toBe("selected-version");
  expect(screen.getByRole("img", { name: "方案 2 / 3" })).toBeTruthy();
});

it("未配置或任务已在进行时，不再发起收费请求", () => {
  const disabled = { ...caps, three_d: { ...caps.three_d!, enabled: false } };
  const { rerender } = render(<Model3DPanel version={version} title="方案 2 / 3" caps={disabled} />);
  expect(screen.queryByRole("button", { name: /制作这款的 3D 预览/ })).toBeNull();
  expect(screen.getByText("3D 预览待接通")).toBeTruthy();
  rerender(<Model3DPanel version={version} title="方案 2 / 3" caps={caps} model={{ id: "m1", version_id: version.id, status: "running", estimated_cost_fen: 180 }} />);
  expect(screen.queryByRole("button", { name: /制作这款的 3D 预览/ })).toBeNull();
  expect(screen.getByText(/正在为这款生成真正的 3D 模型/)).toBeTruthy();
  expect(agentApi.generate3D).not.toHaveBeenCalled();
});

it("已保存模型时出现原位查看入口和GLB下载", () => {
  render(<Model3DPanel version={version} title="方案 2 / 3" caps={caps} model={{ id: "m1", version_id: version.id, status: "succeeded", estimated_cost_fen: 180 }} />);
  expect(screen.getByRole("button", { name: "旋转查看 3D" })).toBeTruthy();
  expect(screen.getByRole("img", { name: "方案 2 / 3" })).toBeTruthy();
});
