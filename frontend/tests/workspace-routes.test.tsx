import { expect, it, vi } from "vitest";
import Home from "@/app/page";
import ProjectPage from "@/app/projects/[projectId]/page";
vi.mock("next/navigation", () => ({ redirect: (url: string) => { throw new Error(`redirect:${url}`); } }));
it("旧query只跳准确独立项目并保留handoff，不新建或执行任务", async () => {
  await expect(Home({ searchParams: Promise.resolve({ project: "3", handoff: "fixture-requirement" }) })).rejects.toThrow("redirect:/projects/3?handoff=fixture-requirement");
});
it("非法或不安全整数项目链接不跳，正常首页与项目入口分开", async () => {
  const invalid = await Home({ searchParams: Promise.resolve({ project: "9007199254740992" }) }); expect(invalid.type).toBe("main");
  const invalidRoute = await ProjectPage({ params: Promise.resolve({ projectId: "../3" }) }); expect(invalidRoute.type).toBe("main");
  const home = await Home({ searchParams: Promise.resolve({}) }); const project = await ProjectPage({ params: Promise.resolve({ projectId: "3" }) }); expect(home.type).not.toBe(project.type); expect(project.props.projectId).toBe(3);
});
