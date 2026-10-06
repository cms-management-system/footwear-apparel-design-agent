"use client";
import { appPath } from "@/lib/app-path";

import { useCallback, useEffect, useRef, useState } from "react";
import { activeTask, agentApi, blankSpec, statusLabel, type Capabilities, type Constraint, type DesignSpec, type SpecRecord, type Reference, type Workspace } from "@/lib/agent-api";
import styles from "./design.module.css";
import DesignStart, { type StartInput } from "./DesignStart";

const roleLabels = { structure: "结构与廓形", fabric: "面料外观", color: "配色", detail: "局部细节" };
const kindLabels = { must_keep: "必须保留", may_change: "允许改动", forbidden: "禁止出现", preference: "风格偏好" };
const toolLabels: Record<string, string> = { plan: "安排下一步", inspect_assets: "识别参考素材", propose_spec: "整理设计要求", generate_design: "生成候选图", edit_design: "修改设计", compare_design: "对照要求检查", ask: "澄清需求", finish: "提交评审" };

export default function DesignAgentPanel({ projectId, embedded = false, snapshot, lockedExternally = false, onDirtyChange, onBusyChange }: {
  projectId?: number; embedded?: boolean; snapshot?: Workspace; lockedExternally?: boolean; onDirtyChange?: (dirty: boolean) => void; onBusyChange?: (busy: boolean) => void;
} = {}) {
  const [pid, setPid] = useState<number | null>(null);
  const [projects, setProjects] = useState<{ id: number; name: string }[]>([]);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [draft, setDraft] = useState<DesignSpec>(blankSpec);
  const source = useRef<string | null>(null);
  const selectedPid = useRef<number | null>(null);
  const dirtyRef = useRef(false);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const pendingStart = useRef<{ id: number; uploads: Record<string, string> } | null>(null);
  const [authorized, setAuthorized] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [region, setRegion] = useState("");
  const [base, setBase] = useState("");
  const current = workspace?.specs.find(s => s.id === workspace.head.spec_id);
  const task = workspace?.tasks.at(-1);
  const locked = busy || lockedExternally || activeTask(task);

  const apply = useCallback((data: Workspace) => {
    setWorkspace(data);
    const saved = data.specs.find(s => s.id === data.head.spec_id);
    if (saved && source.current !== saved.id && !dirtyRef.current) {
      setDraft(saved.spec); source.current = saved.id; setAuthorized(false);
    }
  }, []);

  useEffect(() => { if (snapshot) apply(snapshot); }, [snapshot, apply]);

  const reload = useCallback(async () => { const id = selectedPid.current; if (id) { const data = await agentApi.workspace(id); if (selectedPid.current === id) apply(data); } }, [apply]);
  const bootstrap = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [cap, list] = await Promise.all([agentApi.capabilities(), agentApi.projects()]);
      setCaps(cap); setProjects(list.items);
      const query = projectId ?? Number(new URLSearchParams(window.location.search).get("project"));
      if (query > 0) { selectedPid.current = query; setPid(query); }
    } catch (e) { setError(e instanceof Error ? e.message : "连接失败"); }
    finally { setLoading(false); }
  }, [projectId]);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    if (!pid) return;
    let gone = false, timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    const poll = async () => {
      try {
        const data = await agentApi.workspace(pid);
        if (gone) return;
        apply(data); failures = 0;
        if (data.tasks.some(t => ["queued", "running"].includes(t.status))) timer = setTimeout(poll, document.hidden ? 15000 : 2500);
      } catch {
        if (gone) return;
        setError("连接中断，任务不一定停止。已保存内容仍在；正在重新连接。");
        timer = setTimeout(poll, Math.min(5000 * 2 ** failures++, 60000));
      }
    };
    void poll();
    return () => { gone = true; clearTimeout(timer); };
  }, [pid, task?.id, task?.status, apply]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => { if (dirtyRef.current) event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, []);

  function edit(next: Partial<DesignSpec>) { setDraft(d => ({ ...d, ...next })); setDirty(true); dirtyRef.current = true; setAuthorized(false); }
  function markSaved(id: string, spec: DesignSpec) { source.current = id; setDraft(spec); setDirty(false); dirtyRef.current = false; setAuthorized(false); }
  async function action(fn: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await fn(); await reload(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作失败，请重试"); }
    finally { setBusy(false); }
  }
  function choose(id: number, saved = false) {
    if (!saved && dirty && !window.confirm("要求单尚未保存，切换将丢失本次编辑。继续切换？")) return;
    setWorkspace(null); source.current = null; dirtyRef.current = false; setDirty(false); setDraft(blankSpec());
    setError(""); setNotice(""); setAuthorized(false); setBase(""); setFeedback(""); selectedPid.current = id || null; setPid(id || null);
    const route = window.location.pathname === appPath("/requirements") ? appPath("/requirements") : appPath("/");
    window.history.replaceState(null, "", id ? `${route}?project=${id}` : route);
  }
  async function startDesign(input: StartInput) {
    if (!input.intent || !caps || (caps.understand && !input.authorized)) return;
    // Reuse successfully created project/uploads if a later upload fails.
    if (!pendingStart.current) {
      const created = await agentApi.create(input.intent.slice(0, 40));
      pendingStart.current = { id: created.id, uploads: {} };
    }
    const pending = pendingStart.current;
    const references: Reference[] = [];
    for (const item of input.assets) {
      if (!pending.uploads[item.id]) pending.uploads[item.id] = (await agentApi.upload(pending.id, item.file)).id;
      references.push({ asset_id: pending.uploads[item.id], role: item.role, region: "整体", instruction: "" });
    }
    const requested = { ...blankSpec(), intent: input.intent, references };
    let result: SpecRecord;
    try { result = await agentApi.save(pending.id, requested, null); }
    catch (error) {
      // A lost save response may already have committed. Recover only an identical current draft.
      const persisted = await agentApi.workspace(pending.id).catch(() => null);
      const saved = persisted?.specs.find(s => s.id === persisted.head.spec_id);
      if (!saved || saved.status !== "draft" || !Object.entries(requested).every(([key, value]) =>
        JSON.stringify(saved.spec[key as keyof DesignSpec]) === JSON.stringify(value))) throw error;
      result = saved;
    }
    choose(pending.id, true); markSaved(result.id, result.spec);
    setProjects(items => [...items.filter(p => p.id !== pending.id), { id: pending.id, name: input.intent.slice(0, 40) }]);
    pendingStart.current = null;
    if (caps.understand) {
      const storageKey = `design-request:${pending.id}:${result.id}:understand`;
      const key = sessionStorage.getItem(storageKey) || crypto.randomUUID();
      sessionStorage.setItem(storageKey, key);
      await agentApi.submit(pending.id, result.id, "understand", key);
      sessionStorage.removeItem(storageKey);
      setNotice("需求和素材已保存，助手正在理解；结果会在这份设计中显示。");
    } else {
      setNotice("需求和素材已保存。视觉服务尚未开通，本次没有执行理解；开通后可在这里继续。");
    }
  }
  async function save() {
    if (!pid) return;
    const result = await agentApi.save(pid, draft, source.current);
    markSaved(result.id, result.spec); setNotice("要求单已保存。请核对后确认。");
  }
  async function run(mode: "understand" | "design") {
    if (!pid || !current) return;
    const storageKey = `design-request:${pid}:${current.id}:${mode}`;
    const key = sessionStorage.getItem(storageKey) || crypto.randomUUID();
    sessionStorage.setItem(storageKey, key);
    await agentApi.submit(pid, current.id, mode, key);
    sessionStorage.removeItem(storageKey); setAuthorized(false);
    setNotice("任务已受理，可以离开页面；回来后会恢复状态。");
  }
  function reference(id: string, changes: Partial<Reference>) {
    edit({ references: draft.references.map(r => r.asset_id === id ? { ...r, ...changes } : r) });
  }
  function constraint(id: string, changes: Partial<Constraint>) {
    edit({ constraints: draft.constraints.map(c => c.id === id ? { ...c, ...changes } : c) });
  }

  const Container = embedded ? "section" : "main";
  return <Container className={`${styles.studio} ${embedded ? styles.embedded : !pid ? styles.welcome : ""}`}>
    {!embedded && <><a href={appPath(pid ? `/?project=${pid}` : "/")}>返回设计对话</a>
    <div className={styles.heading}><div><h1>{pid ? "一起推敲这份设计" : "先说说你要做什么样的衣服"}</h1><p>{pid ? "素材、设计要求和每一版修改，都保存在这里。" : "说出想法，或带上你的草图。"}</p></div></div>
    {(pid || projects.length > 0) && <details className={styles.projectPicker}><summary>{pid ? workspace?.project.name ?? "当前设计" : "继续之前的设计"}</summary><div className={styles.projectBar}>
      <label>继续已保存的设计<select aria-label="选择设计项目" value={pid ?? ""} onChange={e => choose(Number(e.target.value))} disabled={busy || !projects.length}><option value="" disabled>选择已保存的设计</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      {pid && <button disabled={busy} onClick={() => choose(0)}>开始另一份设计</button>}
    </div></details>}
    </>}
    {loading && <p role="status">正在连接设计工作区…</p>}
    {error && <div role="alert" className={styles.error}>{error} <button onClick={() => void action(async () => { await bootstrap(); await reload(); })}>重新连接</button></div>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {!embedded && pid && caps && <div className={styles.capability}><b>{caps.design ? "模型通道已配置 · 效果待验收" : caps.understand ? "视觉理解已接通 · 设计生成待验证" : "共创工作区已开放 · 视觉模型待接通"}</b><span>{caps.design ? "真实图片仍需按你的设计要求验收。" : caps.understand ? "上传草图或面料照片后，可以让智能体理解素材并整理设计要求。" : "可以上传素材、建立和确认要求单。接通识图与参考图生成后，即可执行。"}</span></div>}
    {!pid && !loading && <DesignStart caps={caps} busy={busy} onDirty={() => { dirtyRef.current = true; setDirty(true); }} onStart={input => void action(() => startDesign(input))} />}
    {pid && !workspace && !error && <p role="status">正在读取已保存的设计…</p>}
    {workspace && <>
      <div className={styles.columns}>
        <section className={styles.panel} aria-labelledby="materials-title">
          <div className={styles.sectionTitle}><h2 id="materials-title">你的想法</h2></div>
          <details open={embedded ? undefined : true}><summary>参考素材</summary><label className={styles.upload}>添加参考图<input type="file" accept="image/png,image/jpeg,image/webp" disabled={locked} onChange={e => { const file = e.target.files?.[0]; e.target.value = ""; if (!file) return; void action(async () => { if (file.size > 10 * 1024 * 1024) throw new Error("每张图片须小于 10 MB"); const asset = await agentApi.upload(pid!, file); if (draft.references.length < 6) edit({ references: [...draft.references, { asset_id: asset.id, role: "structure", region: "整体", instruction: "" }] }); }); }} /><small>PNG / JPEG / WebP · 每张 10 MB 内 · 每轮最多使用 6 张</small></label>
          <div className={styles.assets}>{workspace.assets.map(a => { const r = draft.references.find(r => r.asset_id === a.id); return <article key={a.id} className={styles.asset}>
            <a href={agentApi.image(a.id, "asset")} target="_blank" rel="noreferrer"><img src={agentApi.image(a.id, "asset")} alt={a.name} /></a>
            <div><b>{a.name}</b><label><input type="checkbox" checked={!!r} disabled={locked || (!r && draft.references.length >= 6)} onChange={e => edit({ references: e.target.checked ? [...draft.references, { asset_id: a.id, role: "structure", region: "整体", instruction: "" }] : draft.references.filter(r => r.asset_id !== a.id) })} />本轮使用</label>
              {r && <><label>参考用途<select value={r.role} disabled={locked} onChange={e => reference(a.id, { role: e.target.value as Reference["role"] })}>{Object.entries(roleLabels).map(([key, text]) => <option key={key} value={key}>{text}</option>)}</select></label><details><summary>补充图片说明</summary><label>参考部位<input value={r.region} disabled={locked} onChange={e => reference(a.id, { region: e.target.value })} /></label><label>使用说明<input value={r.instruction} disabled={locked} placeholder="只参考纹理，不采用图中款式" onChange={e => reference(a.id, { instruction: e.target.value })} /></label></details></>}
            </div></article>; })}</div></details>
          <label>想做什么样的设计？<textarea rows={embedded ? 3 : 4} maxLength={4000} disabled={locked} value={draft.intent} onChange={e => edit({ intent: e.target.value })} placeholder="例如：按草图保留方领和高腰线，把面料照片的纹理用在裙身上。" /></label>
          {!!draft.constraints.length && <div className={styles.requirements}><h3>请核对这些要求</h3><ul>{draft.constraints.map(c => <li key={c.id}><b>{kindLabels[c.kind]}：</b>{c.text}{c.verification === "physical" && "（需实物验证）"}</li>)}</ul></div>}
          <details className={styles.disclosure}><summary>补充或调整细节{draft.constraints.length ? `（${draft.constraints.length} 条要求）` : "（可选）"}</summary>
          <div className={styles.subheading}><h3>明确每一条要求</h3><button disabled={locked || draft.constraints.length >= 30} onClick={() => edit({ constraints: [...draft.constraints, { id: `c_${crypto.randomUUID().slice(0, 8)}`, kind: "must_keep", text: "", region: "整体", verification: "visual" }] })}>＋ 添加要求</button></div>
          {!draft.constraints.length && <p className={styles.muted}>把领型、腰线、廓形等关键细节逐条写下，便于生成后逐项对照。</p>}
          {draft.constraints.map((c, i) => <div className={styles.constraint} key={c.id}>
            <label>要求 {i + 1}<select aria-label={`要求 ${i + 1} 类型`} value={c.kind} disabled={locked} onChange={e => constraint(c.id, { kind: e.target.value as Constraint["kind"] })}>{Object.entries(kindLabels).map(([k, v]) => <option value={k} key={k}>{v}</option>)}</select></label>
            <label>具体内容<input value={c.text} disabled={locked} maxLength={500} onChange={e => constraint(c.id, { text: e.target.value })} placeholder="保留草图中的方领" /></label>
            <label>部位<input value={c.region} disabled={locked} onChange={e => constraint(c.id, { region: e.target.value })} /></label>
            <label>核验方式<select value={c.verification} disabled={locked} onChange={e => constraint(c.id, { verification: e.target.value as Constraint["verification"] })}><option value="visual">图片可检查</option><option value="physical">需实物或检测</option></select></label>
            <button aria-label={`移除要求 ${i + 1}`} disabled={locked} onClick={() => edit({ constraints: draft.constraints.filter(x => x.id !== c.id) })}>移除</button>
          </div>)}
          <label>希望交付什么？<input disabled={locked} value={draft.deliverables} onChange={e => edit({ deliverables: e.target.value })} /></label>
          {draft.base_version_id && <label>本轮修改部位（底版 {draft.base_version_id.slice(0, 8)}）<input value={draft.edit_region} disabled={locked} onChange={e => edit({ edit_region: e.target.value })} /></label>}
          </details>
          {!!draft.conflicts.length && <label className={styles.error}>待解决的冲突（解决后移除对应行）<textarea value={draft.conflicts.join("\n")} disabled={locked} onChange={e => edit({ conflicts: e.target.value.split("\n").filter(Boolean) })} /></label>}
          {!!draft.assumptions.length && <div className={styles.assumptions}><b>待核对的细节</b>{draft.assumptions.map((a, i) => <p key={i}>{a}</p>)}</div>}
          <div className={styles.actions}><button disabled={locked || !draft.intent.trim() || draft.constraints.some(c => !c.text.trim())} onClick={() => void action(save)}>{dirty ? "保存要求单" : "保存新版本"}</button>{!embedded && <button disabled={locked || dirty || !current || current.status === "confirmed" || !!draft.conflicts.length} onClick={() => void action(async () => { await agentApi.confirmSpec(current!.id); setNotice("设计要求已确认；生成将以这一版为准。"); })}>{current?.status === "confirmed" && !dirty ? "要求单已确认" : "确认这份要求"}</button>}</div>
          <p className={styles.muted}>{dirty ? "有未保存的修改" : current ? embedded ? `已保存 · ${statusLabel(current.status)}` : `已保存 · ${current.id.slice(0, 8)} · ${statusLabel(current.status)}` : "先保存要求单，再交给 Agent 理解或执行。"}</p>
        </section>
        {!embedded && <aside className={styles.panel} aria-labelledby="agent-title">
          <div className={styles.sectionTitle}><h2 id="agent-title">设计助手</h2></div>

          <div className={styles.authorization}><label><input type="checkbox" checked={authorized} disabled={!caps?.understand || busy} onChange={e => setAuthorized(e.target.checked)} />允许本轮将所选图片和要求发送至下方服务</label><small>理解与检查：{caps?.vision_service ?? "待配置"}<br />生成与修改：{caps?.image_service ?? "待配置"}</small></div>
          <div className={styles.actions}><button disabled={locked || dirty || !current || !authorized || !caps?.understand} onClick={() => void action(() => run("understand"))}>开始理解</button><button className={styles.primary} disabled={locked || dirty || current?.status !== "confirmed" || !authorized || !caps?.design} onClick={() => void action(() => run("design"))}>按确认要求开始设计</button></div>
          {caps && !caps.design && <p className={styles.muted}>{caps.understand ? "可先运行「开始理解」；参考图生成通道完成验证后开放设计。" : "当前待接通视觉理解与参考图生成能力，暂不执行模型调用。"}</p>}
          {task ? <div className={styles.task} aria-live="polite"><h3>{statusLabel(task.status)}</h3><p>{task.outcome || "任务受理后将逐步显示真实执行记录。"}</p><small>推理 {task.reasoning_calls}/8 · 图片 {task.image_calls}/3 · 费用估算参考 ¥{(task.reserved_cost_fen / 100).toFixed(2)}（实际以平台账单为准）</small>
            <details><summary>查看执行记录</summary><ol>{task.steps.map(s => <li key={s.id}>{toolLabels[s.tool] ?? "处理任务"}<span>{({ pending: "进行中", done: "已完成", failed: "未完成", unknown: "结果未知" } as Record<string, string>)[s.status] ?? s.status}</span></li>)}</ol></details>
            {task.error && <p className={styles.error}>{task.error.message}</p>}
            {task.observations.map(o => <details key={o.asset_id}><summary>素材观察 · {workspace.assets.find(a => a.id === o.asset_id)?.name ?? o.asset_id.slice(0, 8)}</summary><p>可见：{o.observable_features.join("；") || "无确定观察"}</p><p>推测：{o.inferences.join("；") || "无"}</p><p>未知：{o.unknowns.join("；") || "无"}</p></details>)}
            {task.status === "awaiting_input" && <><ul>{task.questions.map(q => <li key={q}>{q}</li>)}</ul><label>补充说明<textarea value={feedback} onChange={e => setFeedback(e.target.value)} /></label><button disabled={busy || !feedback.trim()} onClick={() => void action(async () => { await agentApi.control(task.id, "input", feedback); setFeedback(""); })}>补充并继续本轮</button><p className={styles.muted}>如需改变硬性要求，请先取消本轮、修改要求单并重新确认。</p></>}
            {activeTask(task) && <button disabled={busy} onClick={() => void action(async () => { await agentApi.control(task.id, "cancel"); })}>取消后续步骤</button>}
            {task.status === "interrupted" && <button disabled={busy || task.steps.some(s => s.status === "unknown")} onClick={() => void action(async () => { await agentApi.control(task.id, "resume"); })}>从已保存步骤恢复</button>}
          </div> : <p className={styles.muted}>理解后，会在这里列出需要你确认的内容。</p>}
          <p className={styles.muted}>面料照片只支持外观参考，无法证明成分、克重、弹性和抗皱性能。</p>
        </aside>}
      </div>
      {!embedded && workspace.versions.length > 0 && <section className={styles.panel} aria-labelledby="versions-title"><div className={styles.sectionTitle}><span>03 / 推敲</span><h2 id="versions-title">设计版本与逐项检查</h2></div>
        {!workspace.versions.length ? <div className={styles.empty}><span>第一张候选图将在这里出现</span><p>保留原图、要求单和每次修改；生成结果经过实际图片检查后，交由你判断。</p></div> : <>
          <div className={styles.versions}>{workspace.versions.map((v, i) => <article key={v.id} className={styles.version}><div className={styles.subheading}><h3>版本 {i + 1}</h3><span>{statusLabel(v.status)}</span></div><a href={agentApi.image(v.id, "version")} target="_blank" rel="noreferrer"><img src={agentApi.image(v.id, "version")} alt={`服装设计版本 ${i + 1}，点击查看原图`} /></a><p>{v.review?.summary ?? "尚未完成视觉检查，不能确认。"}</p>
            {v.review && <div className={styles.check}><b>整体目标 · {({ pass: "符合", deviation: "有偏差", unknown: "无法确认" } as Record<string, string>)[v.review.goal.status] ?? "待核对"}</b><p>{v.review.goal.evidence}</p>{v.parent_version_id && <p>非目标区域：{v.review.preservation.evidence}</p>}</div>}
            {v.review?.checks.map(c => <div key={c.constraint_id} className={styles.check}><b>{({ pass: "符合", deviation: "有偏差", unknown: "无法确认" })[c.status]} · {workspace.specs.find(s => s.id === v.spec_id)?.spec.constraints.find(x => x.id === c.constraint_id)?.text ?? c.constraint_id}</b><p>{c.candidate_region}：{c.evidence}</p>{c.status !== "pass" && v.spec_id === current?.id && current.spec.constraints.find(x => x.id === c.constraint_id)?.verification === "visual" && <button disabled={locked || dirty || ["confirmed", "superseded"].includes(v.status)} onClick={() => { const evidence = window.prompt("请具体指出图片的哪个部位证明这一条已经符合要求（至少 10 个字）。原模型判断仍会保留。"); if (evidence) void action(async () => { await agentApi.correctCheck(v.id, c.constraint_id, evidence); setNotice("已记录你的视觉纠正依据，请继续核对整份设计。"); }); }}>纠正这条视觉判断</button>}</div>)}
            <div className={styles.actions}><button disabled={locked || dirty} onClick={() => { setBase(v.id); setNotice(`已选版本 ${i + 1} 为修改底版，请在下方填写改动。`); }}>以此版继续修改</button><button disabled={locked || dirty || !v.review || v.status === "confirmed" || v.spec_id !== current?.id} onClick={() => void action(async () => { await agentApi.confirmVersion(v.id); setNotice("该设计版本已确认。后续修改将保留这一版。"); })}>确认设计版本</button><a href={agentApi.delivery(v.id)}>下载图片与要求单</a></div></article>)}</div>
          <div className={styles.revision}><h3>继续修改</h3><p>{base ? `当前底版：${base.slice(0, 8)}` : "先选择上方一个版本作为底版。也可以选择历史版本，保留当前要求。"}</p><label>只修改哪个部位？<input value={region} onChange={e => setRegion(e.target.value)} placeholder="例如：左右袖子" /></label><label>希望怎么修改？<textarea value={feedback} onChange={e => setFeedback(e.target.value)} placeholder="例如：把泡泡袖改成简洁短袖，其余保持不变。" /></label><button disabled={locked || dirty || !base || !region.trim() || !feedback.trim() || !current} onClick={() => void action(async () => { const result = await agentApi.revise(base, feedback, region, current!.id); markSaved(result.id, result.spec); setFeedback(""); setRegion(""); setNotice("已继承原要求并新增修改项，请核对冲突、确认后开始下一轮。"); })}>生成本轮修改要求单</button></div>
        </>}
      </section>}
      {!embedded && workspace.tasks.length > 1 && <details className={styles.panel}><summary>历史协作记录（{workspace.tasks.length - 1} 轮）</summary>{workspace.tasks.slice(0, -1).reverse().map(t => <p key={t.id}>{t.id.slice(0, 8)} · {statusLabel(t.status)} · {t.outcome}</p>)}</details>}
    </>}
    {!embedded && <p className={styles.footer}>图片和需求仅在你授权后发送至模型服务。</p>}
  </Container>;
}
