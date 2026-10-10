import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { addPathPrefix } from "next/dist/shared/lib/router/utils/add-path-prefix";
import { appPath, appRelativePath } from "@/lib/app-path";

vi.mock("next/navigation", () => ({ redirect: (url: string) => { throw new Error(`redirect:${url}`); }, usePathname: () => "/v2/design/handoffs" }));
beforeEach(() => { vi.stubEnv("NEXT_PUBLIC_BASE_PATH", "/v2/design"); });
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });
it.each([
  ["/", "/v2/design/"], ["/projects/7?handoff=a%2Fb", "/v2/design/projects/7?handoff=a%2Fb"],
  ["/demo/manager", "/v2/design/demo/manager"], ["/api/design-auth/me", "/v2/design/api/design-auth/me"],
  ["/v2/design", "/v2/design"], ["/v2/design?demo_role=manager", "/v2/design?demo_role=manager"],
  ["/v2/design#history", "/v2/design#history"], ["/v2/design/projects/7", "/v2/design/projects/7"],
  ["https://example.org/v2/product", "https://example.org/v2/product"], ["//example.org/file", "//example.org/file"],
])("普通导航/API路径 %s 保持一次prefix", (path, expected) => { expect(appPath(path)).toBe(expected); });
it.each([["/v2/design", "/"], ["/v2/design/", "/"], ["/v2/design/projects/7", "/projects/7"], ["/projects/7", "/projects/7"], ["/v2/design-other", "/v2/design-other"]])("浏览器/Next路由 %s 的边界判断不串prefix", (path, expected) => { expect(appRelativePath(path)).toBe(expected); });
it("空basePath保留本机导航与API地址", () => {
  vi.stubEnv("NEXT_PUBLIC_BASE_PATH", ""); expect(appPath("/projects/7")).toBe("/projects/7"); expect(appPath("/api/design-auth/me")).toBe("/api/design-auth/me"); expect(appRelativePath("/")).toBe("/");
});
it("真实team请求与重命名/删除使用prefix而保留原动作上下文，bootstrap不伪造context", async () => {
  vi.resetModules(); const { teamApi, teamRequest, bindDesignContext, jsonBody } = await import("@/lib/team-api");
  const u = { username: "public-synthetic", display_name: "演示设计员工", role: "designer" as const, integration_mode: "managed" as const, instance_id: "design-public-20261010", scope_id: "public-three-agent-20261010", auth_context_id: "fixture-public-context", access_mode: "demo" as const, demo_entry_urls: { designer: "/demo/designer" as const, manager: "/demo/manager" as const } };
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(u), { status: 200 }))); vi.stubGlobal("fetch", fetchMock);
  bindDesignContext(null); await teamApi.demoAccess("designer"); expect(new Headers(fetchMock.mock.calls[0][1].headers).has("X-Design-Context")).toBe(false);
  bindDesignContext(u.auth_context_id, u);
  await teamRequest("/design-projects/7", jsonBody({ title: "合成重命名", expected_revision: 1 }, "PATCH"));
  await teamRequest("/design-projects/7/archive", jsonBody({ expected_revision: 2 }));
  expect(fetchMock.mock.calls.map(call => call[0])).toEqual(["/v2/design/api/design-auth/demo-access", "/v2/design/api/design-projects/7", "/v2/design/api/design-projects/7/archive"]);
  for (const call of fetchMock.mock.calls.slice(1)) { const init = call[1]; expect(init.credentials).toBe("same-origin"); expect(new Headers(init.headers).get("X-Design-Context")).toBe(u.auth_context_id); expect(new Headers(init.headers).get("X-Design-Action-Id")).toBeTruthy(); }
  bindDesignContext(null);
});
it("canvas/封面/选中版本/下载与旧模型资源全部指向设计prefix，空base兼容", async () => {
  vi.resetModules(); const { agentApi } = await import("@/lib/agent-api");
  expect(agentApi.image("asset-fixture", "asset")).toBe("/v2/design/api/design-assets/asset-fixture/image");
  expect(agentApi.image("version-fixture", "version")).toBe("/v2/design/api/design-versions/version-fixture/image");
  expect(agentApi.delivery("version-fixture")).toBe("/v2/design/api/design-versions/version-fixture/delivery");
  expect(agentApi.samplePack("version-fixture")).toBe("/v2/design/api/design-versions/version-fixture/sample-pack");
  expect(agentApi.sampleFlat("version-fixture", "front")).toBe("/v2/design/api/design-versions/version-fixture/sample-pack/front.svg");
  expect(agentApi.technicalFlatSvg("flat-fixture")).toBe("/v2/design/api/technical-flats/flat-fixture/front.svg");
  expect(agentApi.model3D("model-fixture")).toBe("/v2/design/api/design-models3d/model-fixture/file");
  vi.stubEnv("NEXT_PUBLIC_BASE_PATH", ""); vi.resetModules(); const local = await import("@/lib/agent-api"); expect(local.agentApi.image("v", "version")).toBe("/api/design-versions/v/image");
});
it("旧入口服务器redirect保持逻辑路径，让已安装Next框架只加一次prefix", async () => {
  const Home = (await import("@/app/page")).default; const Design = (await import("@/app/design/page")).default;
  const Requirements = (await import("@/app/requirements/page")).default; const Workbench = (await import("@/app/workbench/page")).default;
  const cases = [
    [() => Home({ searchParams: Promise.resolve({ project: "7", handoff: "synthetic/a" }) }), "/projects/7?handoff=synthetic%2Fa"],
    [() => Design({ searchParams: Promise.resolve({ project: "7" }) }), "/projects/7"],
    [() => Requirements(), "/handoffs"], [() => Workbench(), "/"],
  ] as const;
  for (const [run, target] of cases) {
    let message = ""; try { await run(); } catch (cause) { message = (cause as Error).message; }
    expect(message).toBe(`redirect:${target}`); expect(addPathPrefix(target, "/v2/design")).toBe(`/v2/design${target}`);
  }
});
it("Next配置保留basePath且API外部destination仍是后端root，不重复添加prefix", async () => {
  vi.stubEnv("BACKEND_BASE_URL", "http://127.0.0.1:8192"); vi.stubEnv("NEXT_DIST_DIR", ".next-public-design"); vi.resetModules();
  const config = (await import("../next.config")).default;
  expect(config.basePath).toBe("/v2/design"); expect(config.distDir).toBe(".next-public-design");
  expect(await config.rewrites!()).toEqual([{ source: "/api/:path*", destination: "http://127.0.0.1:8192/api/:path*" }]);
});
it("免登录、旧登录门禁与角色重定向保留公网prefix和合法角色query", async () => {
  vi.resetModules(); const replace = vi.fn(); vi.stubGlobal("window", { location: { replace } });
  const { navigateDemo, navigateDemoEntry, navigateTeamWorkspace } = await import("@/lib/demo-access");
  navigateDemo("designer"); navigateDemo("manager"); navigateDemoEntry("manager"); navigateTeamWorkspace("manager");
  expect(replace.mock.calls.map(call => call[0])).toEqual(["/v2/design/?demo_role=designer", "/v2/design/handoffs?demo_role=manager", "/v2/design/demo/manager", "/v2/design/handoffs"]);
});
it("跨产品链接指向各自同Origin prefix，不叠加设计prefix", async () => {
  const origin = "https://s357brv8j8f9gdhvbu9a9.apigateway-cn-beijing.volceapi.com";
  vi.stubEnv("NEXT_PUBLIC_LINK_OUTFIT", `${origin}/v2/styling`); vi.stubEnv("NEXT_PUBLIC_LINK_PRODUCT", `${origin}/v2/product`); vi.resetModules();
  const PlatformBar = (await import("@/components/design/PlatformBar")).default;
  function hrefs(node: unknown): string[] {
    if (Array.isArray(node)) return node.flatMap(hrefs);
    if (!node || typeof node !== "object" || !("props" in node)) return [];
    const props = node.props as { href?: unknown; children?: unknown };
    return [...(typeof props.href === "string" ? [props.href] : []), ...hrefs(props.children)];
  }
  const output = hrefs(PlatformBar()).join(" ");
  expect(output).toContain(`${origin}/v2/styling`); expect(output).toContain(`${origin}/v2/product`);
  expect(output).not.toContain("/v2/design/v2/styling"); expect(output).not.toContain("/v2/design/v2/product");
});
