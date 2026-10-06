import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import CmsImport from "@/components/design/CmsImport";
import { agentApi, type CmsPackage } from "@/lib/agent-api";
import { requirementFromPackage } from "@/lib/cms";

vi.mock("@/lib/agent-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/agent-api")>();
  return { ...original, agentApi: { ...original.agentApi, cmsPackage: vi.fn(), cmsPackages: vi.fn() } };
});

const pkg = (): CmsPackage => ({
  package_id: "PKG-20260927-0001",
  signal_ids: ["SIG-1", "SIG-2"],
  signal_count: 2,
  requirement_desc: "做一条方领连衣裙，适合通勤。",
  constraints: ["保留方领", { text: "裙长到膝" }],
  constraints_text: "保留方领\n裙长到膝",
  style_demand: "简洁通勤",
  version: 3,
  status: "approved",
  raw: { package_id: "PKG-20260927-0001", extra: "keep" },
});

afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("空空间自动读取，回到页面时同步新批准需求，不自动开始设计", async () => {
  vi.mocked(agentApi.cmsPackages).mockResolvedValueOnce({ items: [], next_offset: null }).mockResolvedValueOnce({ items: [pkg()], next_offset: null });
  const onUse = vi.fn();
  render(<CmsImport busy={false} onUse={onUse} />);
  expect(await screen.findByText(/当前空间暂无已批准/)).toBeTruthy();
  fireEvent.focus(window);
  await screen.findByRole("article", { name: "证据包摘要" });
  expect(onUse).not.toHaveBeenCalled();
  expect(agentApi.cmsPackages).toHaveBeenCalledTimes(2);
});

it("同步失败可重试，分页保持已有需求且不重复，忙碌时不能开始", async () => {
  vi.mocked(agentApi.cmsPackages).mockRejectedValueOnce(new Error("连接 CMS 超时"))
    .mockResolvedValueOnce({ items: [pkg()], next_offset: 20 })
    .mockResolvedValueOnce({ items: [pkg(), { ...pkg(), package_id: "PKG-2" }], next_offset: null });
  render(<CmsImport busy={true} onUse={vi.fn()} />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "重新同步" }));
  await screen.findByRole("article", { name: "证据包摘要" });
  expect((screen.getByRole("button", { name: "用这个需求开始设计" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "加载更多需求" }));
  await waitFor(() => expect(screen.getAllByRole("article", { name: "证据包摘要" })).toHaveLength(2));
  expect(agentApi.cmsPackages).toHaveBeenLastCalledWith(20);
});

it("同时聚焦和刷新不重复请求，卸载后的旧响应不覆盖新页面", async () => {
  let finish!: (value: { items: CmsPackage[]; next_offset: null }) => void;
  vi.mocked(agentApi.cmsPackages).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<CmsImport busy={false} onUse={vi.fn()} />);
  fireEvent.focus(window);
  fireEvent(document, new Event("visibilitychange"));
  expect(agentApi.cmsPackages).toHaveBeenCalledTimes(1);
  view.unmount();
  finish({ items: [pkg()], next_offset: null });
  vi.mocked(agentApi.cmsPackages).mockResolvedValue({ items: [], next_offset: null });
  render(<CmsImport busy={false} onUse={vi.fn()} />);
  await screen.findByText(/当前空间暂无已批准/);
  expect(screen.queryByRole("article")).toBeNull();
});

it("导入证据包后展示可读摘要，并用需求开始设计", async () => {
  vi.mocked(agentApi.cmsPackages).mockResolvedValue({ items: [pkg()], next_offset: null });
  const onUse = vi.fn();
  render(<CmsImport busy={false} onUse={onUse} />);
  expect(screen.queryByLabelText("证据包编号")).toBeNull();
  const card = await screen.findByRole("article", { name: "证据包摘要" });
  expect(card.textContent).toContain("需求描述");
  expect(card.textContent).toContain("做一条方领连衣裙，适合通勤。");
  expect(card.textContent).toContain("约束");
  expect(card.textContent).toContain("保留方领");
  expect(card.textContent).toContain("关联信号数");
  expect(card.textContent).toContain("2");
  expect(card.textContent).toContain("版本");
  expect(card.textContent).toContain("3");
  expect(screen.getByText("查看原始 JSON").closest("details")?.open).toBe(false);
  fireEvent.click(screen.getByText("查看原始 JSON"));
  expect(screen.getByText(/"extra": "keep"/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "用这个需求开始设计" }));
  expect(onUse).toHaveBeenCalledWith(requirementFromPackage(pkg()), "PKG-20260927-0001");
  expect(onUse.mock.calls[0][0]).toContain("做一条方领连衣裙，适合通勤。");
  expect(onUse.mock.calls[0][0]).toContain("保留方领");
});

it("未批准或找不到证据包时显示后端返回的说明", async () => {
  vi.mocked(agentApi.cmsPackages).mockRejectedValue(new Error("这个证据包尚未批准，产品负责人确认后才能使用。"));
  render(<CmsImport busy={false} onUse={vi.fn()} />);
  expect((await screen.findByRole("alert")).textContent).toContain("尚未批准");
  await waitFor(() => expect(screen.queryByRole("article", { name: "证据包摘要" })).toBeNull());
});
