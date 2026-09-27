export type Constraint = { id: string; kind: "must_keep" | "may_change" | "forbidden" | "preference"; text: string; region: string; verification: "visual" | "physical" };
export type Reference = { asset_id: string; role: "structure" | "fabric" | "color" | "detail"; region: string; instruction: string };
export type DesignSpec = { intent: string; references: Reference[]; constraints: Constraint[]; deliverables: string; base_version_id: string | null; edit_region: string; conflicts: string[]; assumptions: string[] };
export type SpecRecord = { revision_feedback?: { text: string; base_spec_id: string }; id: string; status: string; spec: DesignSpec; fingerprint: string };
export type Asset = { id: string; name: string; width: number; height: number };
export type Check = { constraint_id: string; status: "pass" | "deviation" | "unknown"; candidate_region: string; evidence: string };
export type StyleDirection = { id: string; name: string; theme: string; rationale: string; explore: string; color_story: string; structure: string; material_story: string; selected: boolean };
export type StylePlan = { id: string; spec_id: string; source_fingerprint: string; category: "shoe" | "apparel"; status: "draft" | "confirmed" | "superseded"; directions: StyleDirection[] };
export type Version = { task_id?: string; design_index?: number; design_count?: number; style_plan_id?: string; style_direction_id?: string; style_direction?: StyleDirection; id: string; status: string; spec_id: string; parent_version_id: string | null; review: { goal: { status: string; evidence: string }; preservation: { status: string; evidence: string }; checks: Check[]; summary: string } | null };
export type Model3D = { id: string; version_id: string; status: "queued" | "submitting" | "running" | "saving" | "succeeded" | "failed" | "failed_before_submit" | "interrupted"; estimated_cost_fen: number; error?: { code: string; message: string }; model_file?: string | null };
export type SamplingFields = { material: string; color: string; measurements: string; graphic_placement: string; graphic_dimensions: string; construction: string; notes: string };
export type SamplingSheet = { id: string; version_id: string; spec_id: string; previous_sheet_id: string | null; status: "draft"; fields: SamplingFields; basis?: { intent: string; requirements: string[]; visual_review: string } | null };
export type TechnicalFlat = { id: string; version_id: string; spec_id: string; previous_flat_id: string | null; status: "running" | "draft" | "confirmed" | "superseded" | "failed" | "interrupted" | "unsupported"; features: { category: "dress" | "other"; neckline: string; sleeves: string; closure: string; waist: string; skirt: string; visible_details: string[]; uncertain_details: string[] } | null; error?: string; receipt?: { request_id?: string } };
export const blankSamplingFields = (): SamplingFields => ({ material: "", color: "", measurements: "", graphic_placement: "", graphic_dimensions: "", construction: "", notes: "" });
export type Task = { spec_id?: string; mode?: "understand" | "design" | "style"; design_count?: number; direction_version_ids?: string[]; proposed_spec_id?: string | null; live_text?: string; live_summary?: string; pending_action?: { answer?: string; summary?: string } | null; id: string; status: string; outcome: string | null; questions: string[]; reasoning_calls: number; image_calls: number; reserved_cost_fen: number; max_cost_fen: number | null; error?: { code: string; message: string }; steps: { id: string; tool: string; status: string; summary?: string }[]; observations: { asset_id: string; observable_features: string[]; inferences: string[]; unknowns: string[] }[] };
export type ChatMessage = { id: string; created_at: string; role: "user" | "assistant" | "system"; text: string; task_id?: string | null; references?: Reference[]; spec_id?: string | null };
export type Workspace = { messages?: ChatMessage[]; project: { id: number; name: string; cms?: ProjectCms | null }; head: { spec_id?: string; style_plan_id?: string; confirmed_version_id?: string }; assets: Asset[]; specs: SpecRecord[]; style_plans?: StylePlan[]; tasks: Task[]; versions: Version[]; models3d?: Model3D[]; sampling_sheets?: SamplingSheet[]; technical_flats?: TechnicalFlat[] };
export type Capabilities = { image_call_max_fen?: number; reasoning_call_max_fen?: number; understand: boolean; design: boolean; vision_service: string; image_service: string; quality_status: string; note: string; monthly_allocation_fen: number; three_d?: { enabled: boolean; provider: string; estimated_cost_fen: number; monthly_allocation_fen: number; note: string } };
export type CmsPackage = { package_id: string; signal_ids: string[]; signal_count: number; requirement_desc: string; constraints: unknown; constraints_text: string; style_demand: string; dedup_key?: string | null; version?: number | string | null; status?: string | null; raw: Record<string, unknown> };
export type ProjectCms = { package_id: string | null; package: CmsPackage | null; design_version_id: string | null; response_ids: string[]; response_status: string | null; cms_status: string | null };
export type CmsResponseResult = { design_version_id: string; response_status: string; cms_status: string; response_ids: string[]; package_id: string; version: number; design_note: string };
export const blankSpec = (): DesignSpec => ({ intent: "", references: [], constraints: [], deliverables: "正面完整服装效果图", base_version_id: null, edit_region: "", conflicts: [], assumptions: [] });
export const labels: Record<string, string> = { queued: "已排队", running: "正在执行", awaiting_input: "需要你补充", awaiting_review: "等待你评审", completed: "本轮完成", failed: "本轮未完成", budget_exhausted: "已达到额度", cancelled: "已取消后续步骤", interrupted: "执行中断", candidate: "尚未检查", ready_for_review: "待设计师确认", needs_revision: "有待解决问题", confirmed: "已确认", superseded: "历史确认版", draft: "待确认要求" };
export const statusLabel = (status: string) => labels[status] ?? "状态待核对";
export const activeTask = (task?: Task) => !!task && ["queued", "running", "awaiting_input"].includes(task.status);
const root = "/api";
async function request<T>(path: string, init?: RequestInit, timeoutMs = 30000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(root + path, { ...init, signal: controller.signal });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.error?.message ?? `请求未完成（${response.status}）`);
    if (!body || typeof body !== "object") throw new Error("服务返回格式异常，请稍后重新连接");
    return body as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw new Error("连接超时，任务可能已受理。请刷新查看；重试将使用同一请求编号。");
    throw error;
  } finally { clearTimeout(timer); }
}
const post = <T>(path: string, value = {}) => request<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
const postLong = <T>(path: string, value = {}) => request<T>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) }, 180000);
export const agentApi = {
  sendMessage: (id: number, data: { text: string; references: Reference[]; expected_spec_id: string | null; idempotency_key: string; authorized: boolean }) => post<ChatMessage>(`/project/${id}/design-messages`, data),
  capabilities: () => request<Capabilities>("/design-agent/capabilities"),
  projects: () => request<{ items: { id: number; name: string }[] }>("/design-projects"),
  create: (name: string, cmsPackageId?: string) => post<{ id: number; cms_package_id?: string | null }>("/project", cmsPackageId ? { name, cms_package_id: cmsPackageId } : { name }),
  cmsPackage: (packageId: string) => request<CmsPackage>(`/cms/packages/${encodeURIComponent(packageId)}`),
  submitCmsResponse: (projectId: number, body: { version_id: string; response_status: "adopted" | "not_adopted" | "need_evidence"; design_note: string }) => post<CmsResponseResult>(`/projects/${projectId}/cms-response`, body),
  workspace: async (id: number) => { const data = await request<Workspace>(`/project/${id}/design-workspace`); if (!data.project || !Array.isArray(data.tasks) || !Array.isArray(data.specs) || !Array.isArray(data.versions) || !Array.isArray(data.assets)) throw new Error("工作区数据异常，请重新连接"); return data; },
  upload: (id: number, file: File) => request<Asset>(`/project/${id}/design-assets?name=${encodeURIComponent(file.name)}`, { method: "POST", headers: { "Content-Type": file.type }, body: file }),
  save: (id: number, spec: DesignSpec, expected_spec_id: string | null) => post<SpecRecord>(`/project/${id}/design-specs`, { ...spec, expected_spec_id }),
  confirmSpec: (id: string) => post<SpecRecord>(`/design-specs/${id}/confirm`),
  submit: (id: number, specId: string, mode: "understand" | "design" | "style", key: string, count = 1, plan?: { id: string; directionIds: string[] }) => post<Task>(`/project/${id}/design-tasks`, { spec_id: specId, mode, idempotency_key: key, authorized: true, design_count: count, max_image_calls: Math.max(3, count), max_reasoning_calls: 8, ...(plan ? { style_plan_id: plan.id, style_direction_ids: plan.directionIds } : {}) }),
  reviseStylePlan: (id: string, directions: StyleDirection[]) => post<StylePlan>(`/style-plans/${id}/revise`, { expected_plan_id: id, directions }),
  confirmStylePlan: (id: string) => post<StylePlan>(`/style-plans/${id}/confirm`),
  control: (id: string, action: "cancel" | "resume" | "input", text = "") => post<Task>(`/design-tasks/${id}/${action}`, action === "input" ? { text } : {}),
  revise: (id: string, text: string, edit_region: string, expected_spec_id: string) => post<SpecRecord>(`/design-versions/${id}/revise`, { text, edit_region, expected_spec_id }),
  correctCheck: (id: string, constraint_id: string, evidence: string) => post<Version>(`/design-versions/${id}/check-corrections`, { constraint_id, status: "pass", evidence }),
  confirmVersion: (id: string) => post<Version>(`/design-versions/${id}/confirm`),
  saveSamplingSheet: (id: string, fields: SamplingFields, expected_sheet_id: string | null) => post<SamplingSheet>(`/design-versions/${id}/sampling-sheet`, { ...fields, expected_sheet_id }),
  autoSamplingSheet: (id: string) => post<SamplingSheet>(`/design-versions/${id}/sampling-sheet/auto`),
  technicalFlat: (id: string) => request<{ item: TechnicalFlat | null }>(`/design-versions/${id}/technical-flat`),
  generateTechnicalFlat: (id: string) => postLong<TechnicalFlat>(`/design-versions/${id}/technical-flat`),
  reviseTechnicalFlat: (id: string, feedback: string) => postLong<TechnicalFlat>(`/technical-flats/${id}/revise`, { feedback }),
  confirmTechnicalFlat: (id: string) => post<TechnicalFlat>(`/technical-flats/${id}/confirm`),
  technicalFlatSvg: (id: string) => `${root}/technical-flats/${id}/front.svg`,
  generate3D: (id: string, key: string) => post<Model3D>(`/design-versions/${id}/model3d`, { authorized: true, idempotency_key: key }),
  model3D: (id: string) => `${root}/design-models3d/${id}/file`,
  image: (id: string, kind: "asset" | "version") => `${root}/design-${kind === "asset" ? "assets" : "versions"}/${id}/image`,
  delivery: (id: string) => `${root}/design-versions/${id}/delivery`,
  samplePack: (id: string) => `${root}/design-versions/${id}/sample-pack`,
  sampleFlat: (id: string, side: "front" | "back") => `${root}/design-versions/${id}/sample-pack/${side}.svg`,
};
