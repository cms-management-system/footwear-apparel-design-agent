"use client";
import { useCallback, useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { agentApi, activeTask, type Capabilities, type Reference, type StyleDirection, type Version, type Workspace } from "@/lib/agent-api";
import DesignAgentPanel from "./DesignAgentPanel";
import VersionActions from "./VersionActions";
import Model3DPanel from "./Model3DPanel";
import DesignStart, { type StartInput } from "./DesignStart";
import StylePlanPanel from "./StylePlanPanel";
import styles from "./design.module.css";

const tools: Record<string, string> = { plan: "正在整理回复", plan_directions: "正在规划不同风格方向", inspect_assets: "正在看参考图", generate_design: "正在生成设计图", edit_design: "正在修改设计图", compare_design: "正在对照要求检查" };
export default function DesignChat() {
  const [pid, setPid] = useState<number | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [projects, setProjects] = useState<{ id: number; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const [turn, setTurn] = useState(0);
  const [designCount, setDesignCount] = useState(3);
  const [planDirty, setPlanDirty] = useState(false);
  const [showLatest, setShowLatest] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [detailsVisited, setDetailsVisited] = useState(false);
  const [detailsDirty, setDetailsDirty] = useState(false);
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [versionDrafts, setVersionDrafts] = useState<Record<string, boolean>>({});
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
  const refresh = useCallback(async (id: number) => { setWorkspace(await agentApi.workspace(id)); }, []);

  useEffect(() => {
    const openHistory = () => { if (window.location.hash === "#history" && historyRef.current) historyRef.current.open = true; };
    openHistory();
    window.addEventListener("hashchange", openHistory);
    const query = Number(new URLSearchParams(window.location.search).get("project"));
    if (Number.isSafeInteger(query) && query > 0) setPid(query);
    Promise.all([agentApi.capabilities(), agentApi.projects()]).then(([cap, list]) => { setCaps(cap); setProjects(list.items); }).catch(() => setError("连接失败，请刷新重试。"));
    const guard = (event: BeforeUnloadEvent) => { if (dirty.current) event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    const trackScroll = () => {
      follow.current = document.documentElement.scrollHeight - window.scrollY - window.innerHeight < 100;
      if (follow.current) setShowLatest(false);
    };
    window.addEventListener("scroll", trackScroll, { passive: true });
    return () => { window.removeEventListener("beforeunload", guard); window.removeEventListener("scroll", trackScroll); window.removeEventListener("hashchange", openHistory); };
  }, []);
  useEffect(() => {
    if (!pid) return;
    let disposed = false, source: EventSource | undefined, timer: ReturnType<typeof setTimeout>;
    const update = (data: Workspace) => { if (!disposed) { setWorkspace(data); setConnection(""); } };
    const connect = () => {
      if (disposed) return;
      if (typeof EventSource === "undefined") {
        agentApi.workspace(pid).then(update).catch(() => { if (!disposed) setConnection("连接中断，正在恢复对话…"); });
        timer = setTimeout(connect, 2500); return;
      }
      source = new EventSource(`/api/project/${pid}/design-events`);
      source.addEventListener("workspace", event => { try { update(JSON.parse((event as MessageEvent).data)); } catch { setConnection("正在重新同步对话…"); } });
      const reconnect = () => { source?.close(); if (!disposed) timer = setTimeout(connect, 1500); };
      source.addEventListener("done", reconnect);
      source.onerror = () => { if (!disposed) setConnection("实时连接中断，正在恢复；当前任务不会重新执行。"); reconnect(); };
    };
    agentApi.workspace(pid).then(update).catch(() => { if (!disposed) setConnection("正在重新连接…"); });
    connect();
    return () => { disposed = true; source?.close(); clearTimeout(timer); };
  }, [pid]);
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
    setVersionDrafts({}); setSelectedVersionId(null); setDetailsOpen(false); setDetailsVisited(false); setDetailsDirty(false); setDetailsBusy(false); setPlanDirty(false);
    setPid(id); setWorkspace(null); setError(""); setConnection(""); dirty.current = false; pending.current = null;
    setTurn(x => x + 1); follow.current = true;
    window.history.replaceState(null, "", id ? `/?project=${id}` : "/");
  }
  async function send(input: StartInput) {
    if (busy || working || detailsDirty || detailsBusy || versionDirty || planDirty) return;
    setBusy(true); setError("");
    try {
      if (!pending.current) pending.current = { pid: pid ?? (await agentApi.create(input.intent.slice(0, 40))).id, uploads: {} };
      const attempt = pending.current;
      const references: Reference[] = [];
      for (const asset of input.assets) {
        if (!attempt.uploads[asset.id]) attempt.uploads[asset.id] = (await agentApi.upload(attempt.pid, asset.file)).id;
        references.push({ asset_id: attempt.uploads[asset.id], role: asset.role, region: "整体", instruction: "" });
      }
      const data = { text: input.intent, references, authorized: input.authorized };
      const signature = JSON.stringify(data);
      if (attempt.signature !== signature) { attempt.signature = signature; attempt.key = crypto.randomUUID(); attempt.expected = current?.id ?? null; }
      await agentApi.sendMessage(attempt.pid, { ...data, expected_spec_id: attempt.expected ?? null, idempotency_key: attempt.key! });
      dirty.current = false; follow.current = true;
      if (!pid) { setPid(attempt.pid); window.history.replaceState(null, "", `/?project=${attempt.pid}`); }
      // Receipt acknowledges delivery; a later refresh failure must not re-enable sending this turn.
      pending.current = null; setTurn(x => x + 1);
      try { await refresh(attempt.pid); setProjects((await agentApi.projects()).items); }
      catch { setConnection("消息已送达，正在恢复显示；请勿重复发送。"); }
    } catch (e) { setError(e instanceof Error ? e.message : "发送失败，内容仍保留，可以重试。"); }
    finally { setBusy(false); }
  }
  async function operate(fn: () => Promise<unknown>) {
    if (busy || !pid) return false;
    setBusy(true); setError("");
    try { await fn(); await refresh(pid); return true; }
    catch (e) { setError(e instanceof Error ? e.message : "操作未完成"); return false; }
    finally { setBusy(false); }
  }
  async function design() {
    if (!pid || !current || !stylePlan || stylePlan.status !== "confirmed" || !caps?.design) return;
    const storage = `design-request:${pid}:${current.id}:design:${stylePlan.id}`;
    const key = sessionStorage.getItem(storage) || crypto.randomUUID(); sessionStorage.setItem(storage, key);
    await agentApi.submit(pid, current.id, "design", key, selectedDirectionIds.length, { id: stylePlan.id, directionIds: selectedDirectionIds }); sessionStorage.removeItem(storage);
  }
  async function planStyle() {
    if (!pid || !current || !caps?.understand) return;
    const storage = `style-request:${pid}:${current.id}:${designCount}`;
    const key = sessionStorage.getItem(storage) || crypto.randomUUID(); sessionStorage.setItem(storage, key);
    await agentApi.submit(pid, current.id, "style", key, designCount);
    sessionStorage.removeItem(storage);
  }
  async function saveStylePlan(directions: StyleDirection[]): Promise<boolean> {
    if (!pid || !stylePlan || busy) return false;
    setBusy(true); setError("");
    try { await agentApi.reviseStylePlan(stylePlan.id, directions); await refresh(pid); setPlanDirty(false); return true; }
    catch (e) { setError(e instanceof Error ? e.message : "方向调整未保存"); return false; }
    finally { setBusy(false); }
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
    return v.review ? "已检查" : "待检查";
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

        {uncertainImageCall && <><p className={styles.muted}>这次出图没有收到图片，也无法确认服务商是否已处理请求。为避免重复调用，已停止自动重试；原要求和已有图片仍在。</p><button disabled={busy} onClick={() => void operate(() => agentApi.control(task.id, "cancel"))}>结束本轮</button><p className={styles.muted}>结束后可手动重新生成，可能产生新的一次调用。</p></>}
        {task.status === "interrupted" && !uncertainImageCall && <button disabled={busy || task.steps.some(s => s.status === "unknown")} onClick={() => void operate(() => agentApi.control(task.id, "resume"))}>恢复本轮</button>}
        {task.status === "awaiting_input" && <button className={styles.quietAction} disabled={busy} onClick={() => void operate(() => agentApi.control(task.id, "cancel"))}>结束本轮</button>}
      </div>;
  const renderVersion = (v: Version, i: number, parentTitle?: string, selected = false): ReactNode => {
    const title = parentTitle ? `${parentTitle} · 修改版 ${i + 1}` : v.design_index ? `方案 ${v.design_index} / ${v.design_count}${v.style_direction?.name ? ` · ${v.style_direction.name}` : ""}` : `设计版本 ${i + 1}`;
    const children = workspace?.versions.filter(child => child.parent_version_id === v.id) ?? [];
    const hasModifiedVersion = current?.spec.base_version_id === v.id
      ? children.some(child => child.spec_id === current.id)
      : children.length > 0;
    return <article id={`version-${v.id}`} key={v.id} className={styles.chatCard}><h2>{title}</h2><div className={selected ? styles.selectedVersionLayout : undefined}>
      <Model3DPanel version={v} title={title} model={workspace?.models3d?.find(m => m.version_id === v.id)} caps={caps} />
      <div className={selected ? styles.selectedVersionInfo : undefined}>
        {v.style_direction && <p className={styles.directionNote}><b>本款方向：</b>{v.style_direction.explore}<br /><span>{v.style_direction.rationale}</span></p>}
        <p>{v.review?.summary ?? "图片已生成，检查结果尚未就绪。"}</p>
        {selected && !!v.review?.checks.length ? <div className={styles.reviewChecklist}><h3>逐项检查</h3><ul>{v.review.checks.map(check => <li key={check.constraint_id}><span>{check.status === "pass" ? "符合" : check.status === "deviation" ? "有偏差" : "待核对"}</span><p>{check.evidence}</p></li>)}</ul></div>
          : <details><summary>逐项检查</summary>{v.review?.checks.map(check => <p key={check.constraint_id}>{check.evidence}</p>)}</details>}
      </div>
    </div><VersionActions key={`${pid}:${v.id}`} version={v} title={title} current={current} projectId={pid!} caps={caps} hasModifiedVersion={hasModifiedVersion} selected={v.status === "confirmed"} confirmableSpecId={summarySpec?.id} awaitingTaskId={task?.status === "awaiting_input" && !busy && !detailsDirty && !detailsBusy && !Object.entries(versionDrafts).some(([id, dirty]) => id !== v.id && dirty) ? task.id : undefined} onReveal={() => { follow.current = false; }} locked={busy || detailsDirty || detailsBusy || Object.entries(versionDrafts).some(([id, dirty]) => id !== v.id && dirty) || activeTask(task) || !!uncertainImageCall} onAction={fn => { follow.current = false; return operate(fn); }} onDirtyChange={onVersionDirty} />{taskBase === v.id && taskStatus}
      {children.map((child, index) => renderVersion(child, index, title, true))}
    </article>;
  };

  return <main className={`${styles.studio} ${styles.chat}`}>
    <div className={styles.chatHeading}>
      <div><span className={styles.eyebrow}>设计共创</span><h1>{pid ? workspace?.project.name ?? "继续你的设计" : "从一个想法开始"}</h1></div>
      <div className={styles.chatTools}>
        {pid && confirmedVersions.length > 0 && <details className={styles.confirmedPicker}>
          <summary>已确认 {confirmedVersions.length} 款</summary>
          <nav className={styles.confirmedMenu} aria-label="已确认款式的方案页">
            {confirmedVersions.map((version, index) => <a key={version.id} href={`/designs/${version.id}?project=${pid}`}>
              <span>{version.design_index ? `方案 ${version.design_index} / ${version.design_count ?? confirmedVersions.length}` : `方案 ${index + 1}`}{version.parent_version_id ? " · 修改版" : ""}</span>
              {version.style_direction?.name && <small>{version.style_direction.name}</small>}
            </a>)}
          </nav>
        </details>}
        {!!projects.length && <details ref={historyRef} id="history" className={styles.projectPicker}><summary>历史对话</summary><select aria-label="选择设计对话" value={pid ?? ""} disabled={busy || detailsBusy} onChange={e => choose(Number(e.target.value))}><option disabled value="">选择一份设计</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></details>}
        {current && !revisionBase && <button ref={detailsTrigger} aria-expanded={detailsOpen} aria-controls="design-details" onClick={toggleDetails}>{detailsOpen ? "收起细节" : stylePlan ? "修改原始需求" : "查看或调整细节"}</button>}
        {pid && <button disabled={busy || detailsBusy} onClick={() => choose(null)}>＋ 新对话</button>}
      </div>
    </div>
    {connection && <p role="status" className={styles.muted}>{connection}</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {pid && !workspace && <p role="status">正在恢复对话…</p>}
    <div className={styles.chatBody}>
    <section className={styles.conversation} aria-label="设计对话">
      {!pid && <div className={styles.chatWelcome}><span className={styles.eyebrow}>一起，把想法变成衣服</span><h2>一张草图，一块面料，<br />或者一个还没成形的想法。</h2><p>告诉我你想做什么，我们边聊边推敲。</p><div className={styles.welcomeHints}><span>探索款式</span><span>推敲草图</span><span>尝试面料与配色</span></div></div>}
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
        <p className={styles.muted}>{detailsDirty ? "有未保存的修改，收起后会保留。保存后可继续发送消息。" : "在这里查看或修改，保存后会更新设计摘要。"}</p>
        {activeTask(task) && <p className={styles.muted}>本轮正在进行，细节暂时只读；可以直接在输入框继续交流。</p>}
        <DesignAgentPanel key={pid} projectId={pid} embedded snapshot={workspace ?? undefined} lockedExternally={busy || versionDirty || activeTask(task)} onDirtyChange={setDetailsDirty} onBusyChange={setDetailsBusy} />
      </section>}
        {current.status !== "confirmed" && <div className={styles.actions}><button disabled={busy || detailsDirty || detailsBusy || versionDirty || planDirty || activeTask(task) || !!current.spec.conflicts.length} onClick={() => void operate(() => agentApi.confirmSpec(current.id))}>确认这份要求</button></div>}
        {current.status === "confirmed" && <div className={styles.nextStep}>
          {!stylePlan && <p role="status"><b>要求已确认并保存。</b></p>}
          {!stylePlan && <><h3>先规划几种设计方向？</h3><p className={styles.muted}>助手先说明每款的设计区别和理由，你确认后再出图。</p>
            <fieldset className={styles.countOptions} disabled={busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)}><legend>方向数量</legend>{[3, 4, 5].map(n => <label key={n}><input type="radio" name="design-count" value={n} checked={designCount === n} onChange={() => chooseCount(n)} /><span>{n} 个</span></label>)}</fieldset>
            <button className={styles.primary} disabled={!caps?.understand || !(caps.reasoning_call_max_fen ?? 0) || busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)} onClick={() => void operate(planStyle)}>规划 {designCount} 个方向</button>
          </>}
          {stylePlan && <StylePlanPanel key={stylePlan.id} plan={stylePlan} locked={busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)} onDirtyChange={setPlanDirty} onSave={saveStylePlan} onConfirm={async () => { await operate(() => agentApi.confirmStylePlan(stylePlan.id)); }} />}
          {stylePlan?.status === "confirmed" && <div className={styles.styleGenerate}>
            <button className={styles.primary} disabled={!caps?.design || busy || detailsDirty || detailsBusy || versionDirty || activeTask(task)} onClick={() => void operate(design)}>生成 {selectedDirectionIds.length} 款</button>
            {!caps?.design && <p className={styles.muted}>图片生成功能尚未启用；方向已经保存，接通后可继续出图。</p>}
          </div>}
        </div>}</>}
      </div>}
      {latestVersions.length > 0 && <section aria-label="本次方案"><h2>本次方案</h2>
        {latestBatchTask?.mode === "design" && batchCount > 1 && <p className={styles.muted}>已生成 {latestVersions.length} / {batchCount} 款。{latestVersions.length === batchCount ? "可以开始比较。" : "已完成的图片已保存。"}</p>}
        {batchInterrupted && latestVersions.length < batchCount && <div className={styles.batchNotice} role="alert"><p>剩余 {batchCount - latestVersions.length} 款出图时连接中断。可以从未完成的这一款继续，已有图片会保留。</p><button className={styles.primary} disabled={busy || working} onClick={() => void operate(() => agentApi.control(latestBatchTask!.id, "resume"))}>继续生成剩余 {batchCount - latestVersions.length} 款</button></div>}
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
      {caps && (!pid || workspace) && <DesignStart key={turn} caps={caps} busy={busy} conversation={!!pid} waitingForReply={task?.status === "awaiting_input"} attachmentBlocked={task?.status === "awaiting_input" && task.mode === "design"} sendBlocked={working || detailsDirty || detailsBusy || versionDirty || planDirty} onDirty={() => { dirty.current = true; }} onStart={input => void send(input)} />}
      {detailsDirty && <p className={styles.muted}>设计细节有未保存的修改。<button onClick={revealDetails}>展开并保存</button></p>}
      {planDirty && <p className={styles.muted}>设计方向有未保存的调整，请先在方向卡片中保存。</p>}
      {versionDirty && <p className={styles.muted}>方案下有未保存的修改，收起后仍保留。请先保存修改要求再继续聊天。</p>}
      {working && <div className={styles.replyStatus}><span>正在推敲，你可以先写下一条…</span><button onClick={() => void operate(() => agentApi.control(task!.id, "cancel"))} disabled={busy}>停止本轮</button></div>}
    </div>
  </main>;
}
