import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import CmsImport from "@/components/design/CmsImport";
import { agentApi, type CmsPackage } from "@/lib/agent-api";
import { requirementFromPackage } from "@/lib/cms";

vi.mock("@/lib/agent-api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/agent-api")>();
  return { ...original, agentApi: { ...original.agentApi, cmsPackage: vi.fn() } };
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

it("导入证据包后展示可读摘要，并用需求开始设计", async () => {
  vi.mocked(agentApi.cmsPackage).mockResolvedValue(pkg());
  const onUse = vi.fn();
  render(<CmsImport busy={false} onUse={onUse} />);
  fireEvent.change(screen.getByLabelText("证据包编号"), { target: { value: "PKG-20260927-0001" } });
  fireEvent.click(screen.getByRole("button", { name: "读取证据包" }));
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
  vi.mocked(agentApi.cmsPackage).mockRejectedValue(new Error("这个证据包尚未批准，产品负责人确认后才能使用。"));
  render(<CmsImport busy={false} onUse={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("证据包编号"), { target: { value: "PKG-1" } });
  fireEvent.click(screen.getByRole("button", { name: "读取证据包" }));
  expect((await screen.findByRole("alert")).textContent).toContain("尚未批准");
  await waitFor(() => expect(screen.queryByRole("article", { name: "证据包摘要" })).toBeNull());
});
