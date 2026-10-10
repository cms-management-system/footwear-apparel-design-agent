"use client";
import { DESIGN_SESSION_INVALIDATED_EVENT, designContextKey } from "@/lib/team-api";
import { appPath } from "@/lib/app-path";
import { useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { agentApi, activeTask, type Capabilities, type Reference, type StyleDirection, type Version, type Workspace } from "@/lib/agent-api";
import DesignAgentPanel from "./DesignAgentPanel";
import ProjectSource from "@/components/team/ProjectSource";
import EngineRecovery from "@/components/team/EngineRecovery";
import SourceRequirements from "@/components/team/SourceRequirements";
import PromptComposer from "./PromptComposer";
import { rememberWorkspace } from "@/lib/engine-api";
import VersionActions from "./VersionActions";
import Model3DPanel from "./Model3DPanel";
import DesignStart, { type StartInput } from "./DesignStart";
import StylePlanPanel from "./StylePlanPanel";
import styles from "./design.module.css";

const tools: Record<string, string> = { plan: "正在整理回复", plan_directions: "正在规划不同风格方向", inspect_assets: "正在看参考图", generate_design: "正在生成设计图", edit_design: "正在修改设计图", compare_design: "正在对照要求检查" };
export default function DesignChat({ readOnly: roleReadOnly = false, canCreate = false }: { readOnly?: boolean; canCreate?: boolean }) {
  const [pid, setPid] = useState<number | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const readOnly = roleReadOnly || (!!workspace?.source_binding?.handoff_id && workspace.source_binding.status !== "assigned");
  const [projects, setProjects] = useState<{ id: number; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const [loadError, setLoadError] = useState("");
  const [sourceOpen, setSourceOpen] = useState(false);
  const [retry, setRetry] = useState(0);
  const context = useRef({ pid: null as number | null, epoch: 0, readOnly });
  context.current.readOnly = readOnly;
  const alive = useRef(true);
  const snapshotGeneration = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; context.current.epoch += 1; }; }, []);
  const [turn, setTurn] = useState(0);
  const [pendingEdited, setPendingEdited] = useState(false);
  const [composerBaseSpecId, setComposerBaseSpecId] = useState<string | null | undefined>(undefined);
  const [designCount, setDesignCount] = useState(3);
  const [planDirty, setPlanDirty] = useState(false);
  const [showLatest, setShowLatest] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [detailsVisited, setDetailsVisited] = useState(false);
  const [detailsDirty, setDetailsDirty] = useState(false);
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [versionDrafts, setVersionDrafts] = useState<Record<string, boolean>>({});
  const [promptRevisionTarget, setPromptRevisionTarget] = useState<string | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const versionDirty = Object.values(versionDrafts).some(Boolean);
  const onVersionDirty = useCallback((id: string, value: boolean) => setVersionDrafts(prev => prev[id] === value ? prev : { ...prev, [id]: value }), []);
  const detailsRef = useRef<HTMLElement>(null);
  const historyRef = useRef<HTMLDetailsElement>(null);
  function revealDetails() {
    follow.current = false;
    setDetailsVisited(true); setDetailsOpen(true);
  }
  const detailsTrigger = useRef<HTMLButtonElement>(null);
  const detailsSummaryTrigger = useRef<HTMLButtonElement>(null);
  function toggleDetails(event: MouseEvent<HTMLButtonElement>) { if (detailsOpen) closeDetails(event.currentTarget); else revealDetails(); }
  function closeDetails(source: HTMLButtonElement) {
    if (source !== detailsTrigger.current && source !== detailsSummaryTrigger.current) {
      (detailsSummaryTrigger.current ?? detailsTrigger.current)?.focus({ preventScroll: true });
    }
    setDetailsOpen(false);
  }
  useEffect(() => { const guard = (event: BeforeUnloadEvent) => { if (versionDirty || planDirty) event.preventDefault(); }; window.addEventListener("beforeunload", guard); return () => window.removeEventListener("beforeunload", guard); }, [versionDirty, planDirty]);
  const pending = useRef<{ pid: number; uploads: Record<string, string>; signature?: string; key?: string; expected?: string | null } | null>(null);
  const dirty = useRef(false);

  const follow = useRef(true);
  const current = workspace?.specs.find(s => s.id === workspace.head.spec_id);
  const composerBaseChanged = composerBaseSpecId !== undefined && composerBaseSpecId !== (current?.id ?? null);
  const composerNeedsRebase = composerBaseChanged && (!pending.current?.key || pendingEdited);
  const stylePlan = workspace?.style_plans?.find(p => p.id === workspace.head.style_plan_id && p.spec_id === current?.id);
  const selectedDirectionIds = stylePlan?.directions.filter(d => d.selected).map(d => d.id) ?? [];
  const task = workspace?.tasks.filter(activeTask).at(-1) ?? workspace?.tasks.at(-1);
  const revisionBase = current?.spec.base_version_id;
  let rootVersion = workspace?.versions.find(v => v.id === revisionBase);
  const visited = new Set<string>();
  while (rootVersion?.parent_version_id && !visited.has(rootVersion.id)) {
    visited.add(rootVersion.id);
    const parent = workspace?.versions.find(v => v.id === rootVersion!.parent_version_id);
    if (!parent) break;
    rootVersion = parent;
  }
  const summarySpec = revisionBase ? workspace?.specs.find(s => s.id === rootVersion?.spec_id) : current;
  const taskBase = workspace?.specs.find(s => s.id === task?.spec_id)?.spec.base_version_id;
  const working = !!task && ["queued", "running"].includes(task.status);
  const uncertainImageCall = task?.status === "interrupted" && task.mode === "design" && task.steps.some(step => step.status === "unknown");
  const messages = workspace?.messages ?? [];
  const confirmedVersions = workspace?.versions.filter(v => v.status === "confirmed") ?? [];
  const hasSummary = !!current && !activeTask(task) && (current.status === "confirmed" || current.spec.constraints.length > 0 || workspace?.tasks.some(t => t.proposed_spec_id === current.id));
  function latest() {
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
    setShowLatest(false);
  }
  const refresh = useCallback(async (id: number, epoch = context.current.epoch) => {
    const generation = snapshotGeneration.current;
    const data = await agentApi.workspace(id);
    if (generation === snapshotGeneration.current && alive.current && context.current.pid === id && context.current.epoch === epoch && data.project.id === id) setWorkspace(prev => (prev?.revision ?? 0) > (data.revision ?? 0) ? prev : data);
  }, []);

  useEffect(() => {
    const openHistory = () => { if (window.location.hash === "#history" && historyRef.current) historyRef.current.open = true; };
    openHistory();
    window.addEventListener("hashchange", openHistory);
    const query = Number(new URLSearchParams(window.location.search).get("project"));
    let disposed = false;
    if (Number.isSafeInteger(query) && query > 0) { context.current.pid = query; setPid(query); }
    Promise.all([agentApi.capabilities(), agentApi.projects()]).then(([cap, list]) => { if (!disposed) { setCaps(cap); setProjects(list.items); } }).catch(() => { if (!disposed) setError("能力或任务列表读取失败，请刷新重试。"); });
    const guard = (event: BeforeUnloadEvent) => { if (dirty.current) event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    const trackScroll = () => {
      follow.current = document.documentElement.scrollHeight - window.scrollY - window.innerHeight < 100;
      if (follow.current) setShowLatest(false);
    };
    window.addEventListener("scroll", trackScroll, { passive: true });
    return () => { disposed = true; window.removeEventListener("beforeunload", guard); window.removeEventListener("scroll", trackScroll); window.removeEventListener("hashchange", openHistory); };
  }, []);
  useEffect(() => {
    if (!pid) return;
    let disposed = false, source: EventSource | undefined, timer: ReturnType<typeof setTimeout>;
    const epoch = context.current.epoch;
    const update = (data: Workspace) => { if (!disposed && context.current.pid === pid && context.current.epoch === epoch && data.project?.id === pid) { snapshotGeneration.current += 1; rememberWorkspace(data); setWorkspace(prev => (prev?.revision ?? 0) > (data.revision ?? 0) ? prev : data); setLoadError(""); setConnection(""); } };
    const connect = () => {
      if (disposed) return;
      if (typeof EventSource === "undefined") {
        agentApi.workspace(pid).then(update).catch(() => { if (!disposed) setConnection("连接中断，正在恢复对话…"); });
        timer = setTimeout(connect, 2500); return;
      }
      source = new EventSource(appPath(`/api/project/${pid}/design-events`));
      source.addEventListener("workspace", event => { try { update(JSON.parse((event as MessageEvent).data)); } catch { if (!disposed) setConnection("正在重新同步对话…"); } });
      const reconnect = () => { source?.close(); if (!disposed) timer = setTimeout(connect, 1500); };
      source.addEventListener("done", reconnect);
      source.addEventListener("error", event => {
        const data = (event as MessageEvent).data;
        if (typeof data !== "string") return;
        try {
          const error = JSON.parse(data).error;
          if (["LOGIN_REQUIRED", "ROLE_FORBIDDEN", "NOT_FOUND", "AUTH_CONTEXT_CHANGED"].includes(error?.code)) {
            disposed = true; source?.close(); clearTimeout(timer); setWorkspace(null); setLoadError("当前设计访问权限已变化，请重新确认账号或选择授权项目。");
            if (error.code !== "NOT_FOUND") window.dispatchEvent(new Event(DESIGN_SESSION_INVALIDATED_EVENT));
          }
        } catch { /* Ordinary network errors retain the saved conversation and reconnect. */ }
      });
      source.onerror = () => { if (!disposed) setConnection("实时连接中断，正在恢复；当前任务不会重新执行。"); reconnect(); };
    };
    setLoadError("");
    const generation = snapshotGeneration.current;
    agentApi.workspace(pid).then(data => { if (generation === snapshotGeneration.current) update(data); }).catch(e => { if (!disposed) setLoadError(e instanceof Error ? e.message : "设计任务读取失败，请重试。"); });
    connect();
    return () => { disposed = true; source?.close(); clearTimeout(timer); };
  }, [pid, retry]);
  useEffect(() => {
    if (projects.length && window.location.hash === "#history" && historyRef.current) historyRef.current.open = true;
  }, [projects.length]);
  useEffect(() => {
    if (follow.current) latest();
    else setShowLatest(true);
  }, [messages.length, task?.live_text, task?.live_summary, task?.status, workspace?.versions.length, current?.status]);
  useEffect(() => {
    let saved = 3;
    try { const value = Number(localStorage.getItem(`design-count:${pid}:${current?.id}`)); if ([3, 4, 5].includes(value)) saved = value; } catch { /* Storage may be disabled. */ }
    setDesignCount(saved);
  }, [pid, current?.id]);
  function chooseCount(value: number) {
    setDesignCount(value);
    try { localStorage.setItem(`design-count:${pid}:${current?.id}`, String(value)); } catch { /* Selection still works without storage. */ }
  }

  function choose(id: number | null) {
    if ((dirty.current || detailsDirty || versionDirty || planDirty) && !window.confirm("还有未发送的消息或未保存的细节，切换后会丢失。继续？")) return;
    setVersionDrafts({}); setSelectedVersionId(null); setPromptRevisionTarget(null); setDetailsOpen(false); setDetailsVisited(false); setDetailsDirty(false); setDetailsBusy(false); setPlanDirty(false);
    setSourceOpen(false);
    context.current = { pid: id, epoch: context.current.epoch + 1, readOnly };
    setPid(id); setWorkspace(null); setError(""); setConnection(""); setLoadError(""); setBusy(false); dirty.current = false; pending.current = null; setPendingEdited(false);
    setTurn(x => x + 1); setComposerBaseSpecId(undefined); follow.current = true;
    window.history.replaceState(null, "", appPath(id ? `/?project=${id}` : "/"));
  }
  async function send(input: StartInput) {
    if (readOnly || !pid || busy || working || composerNeedsRebase || detailsDirty || detailsBusy || versionDirty || planDirty) return;
    const epoch = context.current.epoch;
    const valid = () => alive.current && context.current.pid === pid && context.current.epoch === epoch && !context.current.readOnly;
    setBusy(true); setError("");
    try {
      if (!pending.current) pending.current = { pid, uploads: {} };
      const attempt = pending.current;
      const references: Reference[] = [];
      for (const asset of input.assets) {
        if (!valid()) return;
        if (!attempt.uploads[asset.id]) attempt.uploads[asset.id] = (await agentApi.upload(attempt.pid, asset.file)).id;
        references.push({ asset_id: attempt.uploads[asset.id], role: asset.role, region: "整体", instruction: "" });
      }
      if (!valid()) return;
      const data = { text: input.intent, references, authorized: input.authorized };
      const expected = composerBaseSpecId === undefined ? current?.id ?? null : composerBaseSpecId;
      const signature = JSON.stringify({ ...data, expected_spec_id: expected });
      if (attempt.signature !== signature) { attempt.signature = signature; attempt.key = crypto.randomUUID(); attempt.expected = expected; }
      setPendingEdited(false);
      await agentApi.sendMessage(attempt.pid, { ...data, expected_spec_id: attempt.expected ?? null, idempotency_key: attempt.key! });
      if (!valid()) return;
      dirty.current = false; follow.current = true;
      pending.current = null; setTurn(x => x + 1); setComposerBaseSpecId(undefined);
      try { await refresh(attempt.pid, epoch); }
      catch { if (valid()) setConnection("消息已送达，正在恢复显示；请勿重复发送。"); }
    } catch (e) { if (valid()) setError(e instanceof Error ? e.message : "发送结果待确认，内容仍保留。请先刷新核对；原内容重试沿用同一请求编号。"); }
    finally { if (valid()) setBusy(false); }
  }
  async function operate(fn: () => Promise<unknown>) {
    if (readOnly || busy || !pid) return false;
    const epoch = context.current.epoch;
    const valid = () => alive.current && context.current.pid === pid && context.current.epoch === epoch && !context.current.readOnly;
    setBusy(true); setError("");
    try { await fn(); if (!valid()) return false; await refresh(pid, epoch); return valid(); }
    catch (e) { if (valid()) setError(e instanceof Error ? e.message : "操作未完成，编辑内容已保留。请先核对当前任务状态。"); return false; }
    finally { if (valid()) setBusy(false); }
  }
  async function design() {
    if (readOnly || !pid || !current || !stylePlan || stylePlan.status !== "confirmed" || !caps?.design) return;
    const storage = `${designContextKey()}:design-request:${pid}:${current.id}:design:${stylePlan.id}`;
    const key = sessionStorage.getItem(storage) || crypto.randomUUID(); sessionStorage.setItem(storage, key);
    await agentApi.submit(pid, current.id, "design", key, selectedDirectionIds.length, { id: stylePlan.id, directionIds: selectedDirectionIds }); sessionStorage.removeItem(storage);
  }
  async function planStyle() {
    if (readOnly || !pid || !current || !caps?.understand) return;
    const storage = `${designContextKey()}:style-request:${pid}:${current.id}:${designCount}`;
    const key = sessionStorage.getItem(storage) || crypto.randomUUID(); sessionStorage.setItem(storage, key);
    await agentApi.submit(pid, current.id, "style", key, designCount);
    sessionStorage.removeItem(storage);
  }
  async function saveStylePlan(directions: StyleDirection[]): Promise<boolean> {
    if (readOnly || !pid || !stylePlan || busy) return false;
    const saved = await operate(() => agentApi.reviseStylePlan(stylePlan.id, directions));
    if (saved) setPlanDirty(false);
    return saved;
  }

  const batches: { id: string; versions: Version[] }[] = [];
  for (const version of workspace?.versions.filter(v => !v.parent_version_id || !workspace.versions.some(parent => parent.id === v.parent_version_id)) ?? []) {
    const id = version.task_id ?? workspace?.tasks.find(t => t.direction_version_ids?.includes(version.id))?.id ?? "legacy";
    let batch = batches.find(b => b.id === id);
    if (!batch) { batch = { id, versions: [] }; batches.push(batch); }
    batch.versions.push(version);
  }
  const latestVersions = batches.at(-1)?.versions ?? [];
  const latestBatchTask = workspace?.tasks.find(t => t.id === batches.at(-1)?.id);
  const batchCount = latestBatchTask?.design_count ?? latestVersions[0]?.design_count ?? latestVersions.length;
  const batchInterrupted = latestBatchTask?.status === "interrupted" && latestBatchTask.steps.some(step => step.status === "unknown" && ["generate_design", "edit_design"].includes(step.tool));
  const selectedVersion = latestVersions.find(v => v.id === selectedVersionId)
    ?? latestVersions.find(v => v.id === rootVersion?.id)
    ?? latestVersions.find(v => v.status === "confirmed")
    ?? latestVersions[0];
  const previewStatus = (v: Version) => {
    if (versionDrafts[v.id]) return "有未保存修改";
    if (revisionBase === v.id) {
      if (working) return "正在修改这款";
      return workspace?.versions.some(child => child.parent_version_id === v.id && child.spec_id === current?.id) ? "已有修改版" : "有修改要求";
    }
    if (v.status === "confirmed") return "已确认";
    return v.execution_mode === "image_only" ? "待人工核对" : v.review ? "已检查" : "待检查";
  };
  const taskStatus = task && <div className={styles.assistantMessage} aria-live="polite" aria-atomic="false">
        {working && task.mode !== "style" && task.design_count && task.design_count > 1 && <p>已生成 {task.direction_version_ids?.length ?? 0} / {task.design_count} 款，正在逐款生成与检查。</p>}
        {working && <small role="status">{task.status === "queued" ? "已收到，正在准备回复…" : tools[task.steps.find(s => s.status === "pending")?.tool ?? "plan"] ?? "正在处理…"}</small>}
        {(task.live_text || task.live_summary) && task.error?.code !== "MODEL_SCHEMA_INVALID" && <><p>{task.live_text || task.live_summary}</p><small>{working ? "正在生成，内容尚未完成" : "未完成的回复片段"}</small></>}
        {task.status === "awaiting_input" && <>{!messages.some(m => m.task_id === task.id && m.text === task.questions.join("\n")) && <p>{task.questions.join("\n")}</p>}</>}
        {task.status === "budget_exhausted" && task.mode === "understand"
          ? <p className={styles.muted}>这段对话现在可以继续了，请在下方发送下一条消息。</p>
          : task.error && task.status !== "cancelled" && <p role="alert" className={styles.error}>{task.error.code === "MODEL_SCHEMA_INVALID" ? "这次回复没有整理成功，你的消息已保存。可以继续补充想法，助手会接着处理。" : task.error.message}</p>}
        {task.status === "cancelled" && <p className={styles.muted}>已停止后续处理，已有内容保留。</p>}
        {!!task.observations.length && <details><summary>查看素材观察</summary>{task.observations.map(o => <div key={o.asset_id}><p>{o.observable_features.join("；")}</p><p className={styles.muted}>待确认：{o.unknowns.join("；")}</p></div>)}</details>}

        {uncertainImageCall && <><p className={styles.muted}>这次出图没有收到图片，也无法确认服务商是否已处理请求。为避免重复调用，已停止自动重试；原要求和已有图片仍在。</p><button disabled={readOnly || busy} onClick={() => void operate(() => agentApi.control(task.id, "cancel"))}>结束本轮</button><p className={styles.muted}>结束后可手动重新生成，可能产生新的一次调用。</p></>}
        {task.status === "interrupted" && task.mode !== "image_only" && !uncertainImageCall && <button disabled={readOnly || !caps?.understand || (task.mode === "design" && !caps?.design) || busy || task.steps.some(s => s.status === "unknown")} onClick={() => void operate(() => agentApi.control(task.id, "resume"))}>恢复本轮</button>}
        {task.status === "awaiting_input" && <button className={styles.quietAction} disabled={readOnly || busy} onClick={() => void operate(() => agentApi.control(task.id, "cancel"))}>结束本轮</button>}
      </div>;
  const renderVersion = (v: Version, i: number, parentTitle?: string, selected = false): ReactNode => {
    const title = parentTitle ? `${parentTitle} · 修改版 ${i + 1}` : v.design_index ? `方案 ${v.design_index} / ${v.design_count}${v.style_direction?.name ? ` · ${v.style_direction.name}` : ""}` : `设计版本 ${i + 1}`;
    const children = workspace?.versions.filter(child => child.parent_version_id === v.id) ?? [];
    const hasModifiedVersion = current?.spec.base_version_id === v.id
      ? children.some(child => child.spec_id === current.id)
      : children.length > 0;
    return <article id={`version-${v.id}`} key={v.id} className={styles.chatCard}><h2>{title}</h2><div className={selected ? styles.selectedVersionLayout : undefined}>
      <Model3DPanel readOnly={readOnly} version={v} title={title} model={workspace?.models3d?.find(m => m.version_id === v.id)} caps={caps} />
      <div className={selected ? styles.selectedVersionInfo : undefined}>
        {v.style_direction && <p className={styles.directionNote}><b>本款方向：</b>{v.style_direction.explore}<br /><span>{v.style_direction.rationale}</span></p>}
        <p>{v.review?.summary ?? (v.execution_mode === "image_only" ? "图片已保存。未执行付费识图检查，请人工核对设计细节。" : working ? "图片已保存，正在处理检查任务。" : "图片已保存，自动检查未完成。请在下方重新检查已有图片。")}</p>
        {selected && !!v.review?.checks.length ? <div className={styles.reviewChecklist}><h3>逐项检查</h3><ul>{v.review.checks.map(check => <li key={check.constraint_id}><span>{check.status === "pass" ? "符合" : check.status === "deviation" ? "有偏差" : "待核对"}</span><p>{check.evidence}</p></li>)}</ul></div>
          : v.review && <details><summary>逐项检查</summary>{v.review.checks.map(check => <p key={check.constraint_id}>{check.evidence}</p>)}</details>}
      </div>
    </div><VersionActions readOnly={readOnly} key={`${pid}:${v.id}`} version={v} title={title} onPromptRevision={workspace?.project_context ? () => { setPromptRevisionTarget(v.id); document.getElementById("image-prompt-editor")?.scrollIntoView({ block: "center", behavior: "auto" }); } : undefined} current={current} projectId={pid!} caps={caps} hasModifiedVersion={hasModifiedVersion} selected={v.status === "confirmed"} confirmableSpecId={summarySpec?.id} awaitingTaskId={task?.status === "awaiting_input" && !busy && !detailsDirty && !detailsBusy && !Object.entries(versionDrafts).some(([id, dirty]) => id !== v.id && dirty) ? task.id : undefined} onReveal={() => { follow.current = false; }} locked={readOnly || busy || detailsDirty || detailsBusy || Object.entries(versionDrafts).some(([id, dirty]) => id !== v.id && dirty) || activeTask(task) || !!uncertainImageCall} onAction={fn => { follow.current = false; return operate(fn); }} onDirtyChange={onVersionDirty} />{taskBase === v.id && taskStatus}
      {children.map((child, index) => renderVersion(child, index, title, true))}
    </article>;
  };

  return <main className={`${styles.studio} ${styles.chat}`}>
    <div className={styles.chatHeading}>
      <div><span className={styles.eyebrow}>设计共创</span><h1>{pid ? workspace?.project.name ?? "正在打开设计任务" : "设计工作区"}</h1>
        {workspace && <p className={styles.muted}>项目 #{pid} · {workspace.project_context?.source_mode === "independent" ? "自主设计" : "上游需求"} · 修订 {workspace.revision ?? "待核对"} · 当前要求版本 {current?.id ?? "尚无要求版本"}</p>}
      </div>
      <div className={styles.chatTools}>
        {canCreate && <a className={styles.pageLink} href={appPath("/new")}>＋ 创建设计</a>}
        <a href={appPath("/projects")}>设计项目</a>
        {pid && workspace && <button disabled={busy} onClick={() => void refresh(pid).catch(cause => setError(cause instanceof Error ? cause.message : "读取失败"))}>刷新当前设计</button>}
        {pid && confirmedVersions.length > 0 && <details className={styles.confirmedPicker}>
          <summary>已确认 {confirmedVersions.length} 款</summary>
          <nav className={styles.confirmedMenu} aria-label="已确认款式的方案页">
            {confirmedVersions.map((version, index) => <a key={version.id} href={appPath(`/designs/${version.id}?project=${pid}`)}>
              <span>{version.design_index ? `方案 ${version.design_index} / ${version.design_count ?? confirmedVersions.length}` : `方案 ${index + 1}`}{version.parent_version_id ? " · 修改版" : ""}</span>
              {version.style_direction?.name && <small>{version.style_direction.name}</small>}
            </a>)}
          </nav>
        </details>}
        {!!projects.length && <details ref={historyRef} id="history" className={styles.projectPicker}><summary>历史对话</summary><select aria-label="选择设计对话" value={pid ?? ""} disabled={busy || detailsBusy} onChange={e => choose(Number(e.target.value))}><option disabled value="">选择一份设计</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></details>}
        {current && !revisionBase && <button ref={detailsTrigger} aria-expanded={detailsOpen} aria-controls="design-details" onClick={toggleDetails}>{detailsOpen ? "收起细节" : readOnly ? "查看设计细节" : stylePlan ? "修改原始需求" : "查看或调整细节"}</button>}
      </div>
    </div>
    {pid && workspace?.project.id === pid && workspace.project_context?.source_mode !== "independent" && <details className={styles.sourcePanel} open={sourceOpen} onToggle={event => setSourceOpen(event.currentTarget.open)}><summary>查看完整上游需求与设计提示词</summary>{sourceOpen && (workspace.source_binding ? <SourceRequirements binding={workspace.source_binding} /> : <ProjectSource projectId={pid} />)}</details>}
    {readOnly && <p className={styles.readOnlyNote} role="status">当前设计为只读查看。请从需求收件查看分派与审查状态；员工确认不代表管理批准。</p>}
    {pid && workspace && <EngineRecovery key={pid} projectId={pid} onResolved={() => refresh(pid)} />}
    {connection && <p role="status" className={styles.muted}>{connection}</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {loadError && <div role="alert" className={styles.error}><p>{loadError}</p><button onClick={() => setRetry(value => value + 1)}>重新读取任务</button><a href={appPath("/handoffs")}>返回我的任务</a></div>}
    {pid && !workspace && !loadError && <p role="status">正在恢复对话…</p>}
    <div className={`${styles.chatBody} ${styles.workspaceGrid}`}>
    <section className={`${styles.conversation} ${styles.conversationColumn}`} aria-label="设计对话">
      {workspace?.project_context && <PromptComposer key={pid} workspace={workspace} revisionVersionId={promptRevisionTarget} readOnly={readOnly} onSaved={() => refresh(pid!)} onDirtyChange={setDetailsDirty} />}
      {!pid && <div className={styles.chatWelcome}><span className={styles.eyebrow}>自主创意 · 上游定向设计</span><h2>{canCreate ? "从一个设计想法开始" : "查看团队的设计项目"}</h2><p>用自然语言发起你的鞋服设计，也可从已分派的上游任务继续创作。</p>{canCreate && <a className={styles.pageLink} href={appPath("/new")}>＋ 创建设计</a>}<a href={appPath("/projects")}>查看设计项目 →</a><a href={appPath("/handoffs")}>查看上游任务 →</a></div>}
      {workspace && !messages.length && current && <article className={styles.userMessage}><small>已有设计要求</small><p>{current.spec.intent}</p></article>}
      {messages.map(m => <article key={m.id} className={m.role === "user" ? styles.userMessage : m.role === "system" ? styles.systemMessage : styles.assistantMessage}>
        {m.role !== "user" && <small>{m.role === "system" ? "系统状态" : "设计助手"}</small>}<p>{m.text}</p>
        {!!m.references?.length && <div className={styles.chatImages}>{m.references.map(ref => <a key={ref.asset_id} href={agentApi.image(ref.asset_id, "asset")} target="_blank" rel="noreferrer"><img src={agentApi.image(ref.asset_id, "asset")} alt={workspace?.assets.find(a => a.id === ref.asset_id)?.name ?? "参考图"} /></a>)}</div>}
      </article>)}
      {!taskBase && !(task?.mode === "design" && task.design_count && task.design_count > 1 && task.status === "interrupted") && taskStatus}
      {current && summarySpec && (hasSummary || detailsVisited) && <div className={styles.chatCard} hidden={!hasSummary && !detailsOpen}><span className={styles.eyebrow}>设计笔记</span><h2>设计要求</h2><p>{summarySpec.spec.intent}</p><ul>{summarySpec.spec.constraints.map(c => <li key={c.id}>{c.text}{c.verification === "physical" ? "（需实物验证）" : ""}</li>)}</ul>
        {summarySpec.spec.conflicts.map((c, i) => <p className={styles.error} key={i}>{c}</p>)}
        {summarySpec.spec.assumptions.map((c, i) => <p className={styles.muted} key={i}>待核对：{c}</p>)}
        {!revisionBase && <>
      {pid && detailsVisited && <section ref={detailsRef} id="design-details" aria-label="设计细节" hidden={!detailsOpen} className={styles.inlineDetails}>
        <div className={styles.subheading}><h2>设计细节</h2><button onClick={e => closeDetails(e.currentTarget)}>收起，继续聊天</button></div>
        <p className={styles.muted}>{readOnly ? "当前以管理者身份只读查看设计依据。" : detailsDirty ? "有未保存的修改，收起后会保留。保存后可继续发送消息。" : "在这里查看或修改，保存后会更新设计摘要。"}</p>
        {activeTask(task) && <p className={styles.muted}>本轮正在进行，细节暂时只读；可以直接在输入框继续交流。</p>}
        {!readOnly && <DesignAgentPanel key={pid} projectId={pid} embedded snapshot={workspace ?? undefined} lockedExternally={busy || versionDirty || activeTask(task)} onDirtyChange={setDetailsDirty} onBusyChange={setDetailsBusy} />}
        {readOnly && <div><p>{summarySpec.spec.intent}</p><p>{summarySpec.spec.deliverables}</p><ul>{summarySpec.spec.constraints.map(item => <li key={item.id}>{item.text}</li>)}</ul></div>}
      </section>}
        {current.status !== "confirmed" && <div className={styles.actions}><button disabled={readOnly || busy || detailsDirty || detailsBusy || versionDirty || planDirty || activeTask(task) || !!current.spec.conflicts.length} onClick={() => void operate(() => agentApi.confirmSpec(current.id))}>确认这份要求</button></div>}
        {current.status === "confirmed" && !workspace?.project_context && <div className={styles.nextStep}>
          {!stylePlan && <p role="status"><b>要求已确认并保存。</b></p>}
          {!stylePlan && <><h3>先规划几种设计方向？</h3><p className={styles.muted}>助手先说明每款的设计区别和理由，你确认后再出图。</p>
            <fieldset className={styles.countOptions} disabled={readOnly || busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)}><legend>方向数量</legend>{[3, 4, 5].map(n => <label key={n}><input type="radio" name="design-count" value={n} checked={designCount === n} onChange={() => chooseCount(n)} /><span>{n} 个</span></label>)}</fieldset>
            <button className={styles.primary} disabled={readOnly || !caps?.understand || !(caps.reasoning_call_max_fen ?? 0) || busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)} onClick={() => void operate(planStyle)}>规划 {designCount} 个方向</button>
          </>}
          {stylePlan && <StylePlanPanel readOnly={readOnly} key={`style:${pid}`} plan={stylePlan} locked={readOnly || busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)} onDirtyChange={setPlanDirty} onSave={saveStylePlan} onConfirm={async () => { await operate(() => agentApi.confirmStylePlan(stylePlan.id)); }} />}
          {stylePlan?.status === "confirmed" && <div className={styles.styleGenerate}>
            <button className={styles.primary} disabled={readOnly || !caps?.design || busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)} onClick={() => void operate(design)}>生成 {selectedDirectionIds.length} 款</button>
            {!caps?.design && <p className={styles.muted}>图片生成功能尚未启用；方向已经保存，接通后可继续出图。</p>}
          </div>}
        </div>}</>}
      </div>}
    </section>
    <section className={styles.previewColumn} aria-label="方案画布">
      {!!workspace && !latestVersions.length && <div className={styles.chatCard}><span className={styles.eyebrow}>方案画布</span><h2>等待设计方案</h2><p>当前任务尚无已保存方案。生成服务未启用时，不会自动出图。</p></div>}
      {latestVersions.length > 0 && <section aria-label="本次方案"><h2>本次方案</h2>
        {latestBatchTask?.mode === "design" && batchCount > 1 && <p className={styles.muted}>已生成 {latestVersions.length} / {batchCount} 款。{latestVersions.length === batchCount ? "可以开始比较。" : "已完成的图片已保存。"}</p>}
        {batchInterrupted && latestVersions.length < batchCount && <div className={styles.batchNotice} role="alert"><p>剩余 {batchCount - latestVersions.length} 款出图时连接中断。可以从未完成的这一款继续，已有图片会保留。</p><button className={styles.primary} disabled={readOnly || !caps?.design || busy || working} onClick={() => void operate(() => agentApi.control(latestBatchTask!.id, "resume"))}>继续生成剩余 {batchCount - latestVersions.length} 款</button></div>}
        {latestVersions.length === batchCount && <p className={styles.muted}>点选后查看大图并修改这一款。</p>}
        <div className={styles.schemePreviews} role="group" aria-label="选择要查看的方案">{latestVersions.map((v, i) => {
          const title = v.design_index ? `方案 ${v.design_index} / ${v.design_count}` : `设计版本 ${i + 1}`;
          return <button key={v.id} type="button" className={styles.schemePreview} aria-pressed={selectedVersion?.id === v.id} aria-label={`查看${title}`} onClick={() => { follow.current = false; setSelectedVersionId(v.id); }}>
            <img src={agentApi.image(v.id, "version")} alt="" loading="lazy" />
            <span className={styles.schemePreviewInfo}><strong>{title}</strong><small>{v.style_direction?.name ? `${v.style_direction.name} · ` : ""}{previewStatus(v)}</small></span>
          </button>;
        })}</div>
        <div className={styles.focusedVersions}>{latestVersions.map((v, i) => <div key={v.id} className={styles.focusedVersion} hidden={selectedVersion?.id !== v.id}>{renderVersion(v, i, undefined, true)}</div>)}</div>
      </section>}
      {batches.length > 1 && <details className={styles.pastDesigns}><summary>历史方案（{batches.length - 1} 批）</summary>{batches.slice(0, -1).map((batch, index) => <section key={batch.id} aria-label={`历史批次 ${index + 1}`}><h3>历史批次 {index + 1}</h3><div className={styles.designResults}>{batch.versions.map((v, i) => renderVersion(v, i))}</div></section>)}</details>}
    </section>

    </div>
    {showLatest && <button className={styles.latest} onClick={() => { follow.current = true; latest(); }}>↓ 查看最新回复</button>}
    <div className={styles.composer}>
      {caps && pid && workspace && !workspace.project_context && !readOnly && <DesignStart key={`${pid}:${turn}`} caps={caps} busy={busy} conversation={!!pid} waitingForReply={task?.status === "awaiting_input"} attachmentBlocked={task?.status === "awaiting_input" && task.mode === "design"} sendBlocked={working || composerNeedsRebase || detailsDirty || detailsBusy || versionDirty || planDirty} onDirty={() => { dirty.current = true; if (pending.current?.key) setPendingEdited(true); setComposerBaseSpecId(value => value === undefined ? current?.id ?? null : value); }} onStart={input => void send(input)} />}
      {composerNeedsRebase && <div role="alert" className={styles.error}><p>设计要求版本已变化，未发送的内容仍保留。请核对当前要求后再继续。</p><button type="button" disabled={busy || readOnly} onClick={() => setComposerBaseSpecId(current?.id ?? null)}>已核对，按当前版本发送</button></div>}
      {detailsDirty && <p className={styles.muted}>设计细节有未保存的修改。<button onClick={revealDetails}>展开并保存</button></p>}
      {planDirty && <p className={styles.muted}>设计方向有未保存的调整，请先在方向卡片中保存。</p>}
      {versionDirty && <p className={styles.muted}>方案下有未保存的修改，收起后仍保留。请先保存修改要求再继续聊天。</p>}
      {working && <div className={styles.replyStatus}><span>{readOnly ? "设计任务正在执行。" : "正在推敲，你可以先写下一条…"}</span><button onClick={() => void operate(() => agentApi.control(task!.id, "cancel"))} disabled={readOnly || busy}>停止本轮</button></div>}
    </div>
  </main>;
}
