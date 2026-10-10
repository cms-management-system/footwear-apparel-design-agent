"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { TeamBoundary } from "@/components/team/TeamSession";
import SyncInbox from "@/components/team/SyncInbox";
import RecordView from "@/components/team/RecordView";
import SourceRequirements from "@/components/team/SourceRequirements";
import { agentApi, type Version } from "@/lib/agent-api";
import { ApiError, errorText, isAbort, teamApi, type TeamMember, type TeamUser } from "@/lib/team-api";
import { canReplay, deliveryStatus, executeAction, freezeAction, label, loadPending, savePending, textField, titleOf, workflowApi, workflowStatus, type FrozenAction, type Handoff } from "@/lib/design-workflow";
import s from "./handoff.module.css";

type Draft = { eventId?: string; note: string; assignee: string; priority: string; due: string; versionId: string; base: number };
const draftFor = (item: Handoff): Draft => ({ note: "", assignee: item.assignee ?? "", priority: "", due: "", versionId: "", base: item.revision });

export default function HandoffsPage() { return <TeamBoundary>{user => <Workspace user={user} />}</TeamBoundary>; }

function Workspace({ user }: { user: TeamUser }) {
  const [items, setItems] = useState<Handoff[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    setSelected(query.get("handoff") ?? "");
    const page = Number(query.get("offset")); if (Number.isSafeInteger(page) && page >= 0) setOffset(page);
    const restore = () => { const q = new URLSearchParams(window.location.search); setSelected(q.get("handoff") ?? ""); setOffset(Number(q.get("offset")) || 0); };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  useEffect(() => {
    const controller = new AbortController(); let current = true;
    setLoading(true); setError("");
    workflowApi.list(offset, controller.signal).then(list => {
      if (!current) return;
      if (!Array.isArray(list.items)) throw new Error("任务列表格式异常，请重新连接。");
      setItems(list.items); setTotal(Number.isFinite(list.total) ? list.total : list.items.length);
    }).catch(cause => { if (current && !isAbort(cause)) { setItems([]); setError(errorText(cause)); } }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; controller.abort(); };
  }, [offset, reload]);
  const dirty = Object.values(drafts).some(d => !!d.note || !!d.due || !!d.priority || !!d.versionId);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    const navigate = (event: MouseEvent) => { const link = (event.target as HTMLElement)?.closest?.("a"); if (dirty && link && !link.href.includes("#") && !window.confirm("尚有未提交的处理意见，离开会丢失这些编辑。继续离开？")) event.preventDefault(); };
    window.addEventListener("beforeunload", warn); document.addEventListener("click", navigate);
    return () => { window.removeEventListener("beforeunload", warn); document.removeEventListener("click", navigate); };
  }, [dirty]);
  const choose = (id: string) => {
    setSelected(id);
    const url = new URL(window.location.href); url.searchParams.set("handoff", id); url.searchParams.set("offset", String(offset)); window.history.pushState({}, "", url);
  };
  const currentId = selected || items[0]?.id;
  const shown = items.filter(item => !filter || item.status === filter);
  const updateDraft = useCallback((id: string, draft: Draft | null) => setDrafts(prev => { const next = { ...prev }; if (draft) next[id] = draft; else delete next[id]; return next; }), []);
  const changed = useCallback(() => setReload(n => n + 1), []);
  return <main className={s.shell}>
    <header className={s.topline}><div><p className={s.kicker}>DESIGN STUDIO / {user.role === "manager" ? "团队工作台" : "我的工作台"}</p><h1>{user.role === "manager" ? "从需求到设计，有据可循。" : "把想法，推进一步。"}</h1><p className={s.subtle}>{user.role === "manager" ? "核对产品批准的要求，分派任务，审查具体方案。" : "打开分派给你的任务，与设计助手一起完善方案。"}</p></div><div className={s.headerActions}>{user.role === "designer" && <Link className={s.primaryLink} href="/new">＋ 创建设计</Link>}<Link href="/projects">设计项目</Link><span>{user.display_name} · {user.role === "manager" ? "设计负责人" : "设计人员"}</span><button onClick={changed} disabled={loading}>{loading ? "正在更新…" : "刷新任务"}</button></div></header>
    {user.role === "manager" && <SyncInbox user={user} onChanged={changed} />}
    <div className={s.toolbar}><div className={s.filters}><label>当前页筛选<select value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部状态</option>{Object.entries(workflowStatus).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label></div><p className={s.subtle}>需求技术接收、设计判断和结果审查分别记录。</p></div>
    {error && <p className={s.error} role="alert">{error} <button onClick={changed}>重新连接</button></p>}
    <div className={s.columns}>
      <section className={s.list} aria-label={user.role === "manager" ? "需求收件箱" : "我的任务"} aria-busy={loading}>
        <h2>{user.role === "manager" ? "需求收件箱" : "我的任务"} <small>{loading ? "…" : total}</small></h2>
        {loading ? <p role="status" className={s.empty}>正在读取任务…</p> : !error && !shown.length ? <div className={s.empty}><h3>{filter ? "当前页没有此状态的任务" : "暂时没有任务"}</h3><p>{user.role === "manager" ? "产品管理团队交接已批准的需求后，会出现在这里。" : "负责人分派任务后，你就可以在这里继续设计。"}</p></div> : shown.map(item => <button key={item.id} className={`${s.listItem} ${currentId === item.id ? s.active : ""}`} aria-current={currentId === item.id ? "true" : undefined} onClick={() => choose(item.id)}><span className={s.listTop}>{item.priority || textField(item.package, "priority", "待定优先级")}<em>{label(item.status)}</em></span><strong>{titleOf(item)}</strong><small>{item.package_id} · 版本 {item.version}</small><small>{item.assignee ? `负责人 ${item.assignee}` : "尚未分派"}</small></button>)}
        <nav className={s.toolbar} aria-label="任务分页"><button disabled={loading || offset === 0} onClick={() => { setOffset(Math.max(0, offset - 20)); setSelected(""); }}>上一页</button><span>{Math.floor(offset / 20) + 1}</span><button disabled={loading || offset + items.length >= total} onClick={() => { setOffset(offset + 20); setSelected(""); }}>下一页</button></nav>
      </section>
      {currentId ? <HandoffDetail key={currentId} id={currentId} user={user} refreshIndex={reload} draft={drafts[currentId]} setDraft={updateDraft} onChanged={changed} /> : <section className={s.detail}><div className={s.empty}><p className={s.kicker}>需求 · 方向 · 方案</p><h2>为下一份设计留好空间</h2><p>选择左侧任务，查看原始要求、版本和下一步。</p></div></section>}
    </div>
  </main>;
}

function HandoffDetail({ id, user, refreshIndex, draft, setDraft, onChanged }: { id: string; user: TeamUser; refreshIndex: number; draft?: Draft; setDraft: (id: string, draft: Draft | null) => void; onChanged: () => void }) {
  const [item, setItem] = useState<Handoff | null>(null);
  const [staff, setStaff] = useState<TeamMember[]>([]);
  const [versions, setVersions] = useState<Version[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<FrozenAction | null>(null);
  const [imageError, setImageError] = useState(false);
  const [operation, setOperation] = useState<Record<string, unknown> | null>(null);
  const mounted = useRef(true);
  const lock = useRef(false);
  const [rejected, setRejected] = useState(false);
  useEffect(() => { mounted.current = true; setPending(loadPending(user, id)); return () => { mounted.current = false; }; }, [id, user]);
  useEffect(() => {
    const controller = new AbortController(); let current = true;
    setLoading(true); setError("");
    workflowApi.detail(id, controller.signal).then(async next => {
      if (!current) return;
      if (next.id !== id || !next.package || !Number.isSafeInteger(next.revision) || !Array.isArray(next.allowed_actions)) throw new Error("任务接口尚未提供完整版本与动作信息，请稍后重新连接。");
      setItem(next);
      if (user.role === "manager") { const people = await teamApi.staff(controller.signal); if (current) setStaff(people.items); }
      else if (next.project_id && next.allowed_actions.includes("submit")) { const workspace = await agentApi.workspace(next.project_id); if (current) setVersions(workspace.versions.filter(v => v.status === "confirmed")); }
    }).catch(cause => { if (current && !isAbort(cause)) { setError(errorText(cause)); if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) setItem(null); } }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; controller.abort(); };
  }, [id, user.role, refreshIndex]);
  const editing = draft ?? (item ? draftFor(item) : null);
  const patch = (change: Partial<Draft>) => { if (editing) setDraft(id, { ...editing, ...change }); };
  const remember = (value: FrozenAction | null) => { setPending(value); savePending(user, id, value); };
  async function run(action: FrozenAction) {
    if (lock.current) return;
    lock.current = true; setRejected(false); setBusy(true); setError(""); setNotice("");
    try {
      await executeAction(action, user);
      if (!mounted.current) return;
      remember(null); setDraft(id, null); setNotice("操作已记录。正在读取当前任务状态。"); onChanged();
    } catch (cause) { if (mounted.current) { setError(errorText(cause)); setRejected(cause instanceof ApiError && [400, 403, 404, 409, 410, 422].includes(cause.status)); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  function act(endpoint: string, value: Record<string, unknown>) {
    if (!item || !editing || pending || lock.current) return;
    try { const action = freezeAction(user, `/design-handoffs/${encodeURIComponent(id)}/${endpoint}`, { ...value, expected_revision: editing.base }); remember(action); void run(action); } catch (cause) { setError(errorText(cause)); }
  }
  async function lookup() {
    if (!pending || busy) return;
    setBusy(true); setError("");
    try { const result = await workflowApi.operation(pending.id); if (mounted.current) { setOperation(result); setNotice("已找到原操作记录。请核对记录与当前任务后结束恢复。"); onChanged(); } }
    catch (cause) { if (mounted.current) setError(cause instanceof ApiError && cause.status === 404 ? "暂未找到原操作记录。原请求仍保留；同一会话下可重试原操作。" : errorText(cause)); }
    finally { if (mounted.current) setBusy(false); }
  }
  if (!item || !editing) return <section className={s.detail} aria-busy={loading}>{loading ? <p role="status">正在读取需求与版本…</p> : <p role="alert" className={s.error}>{error || "任务暂不可用"}</p>}<button disabled={loading} onClick={onChanged}>重新读取</button></section>;
  const stale = editing.base !== item.revision;
  const blocked = busy || loading || !!pending || stale || !user.auth_context_id;
  const allows = (action: string) => item.allowed_actions.includes(action);
  const deliveries = item.deliveries ?? (item.delivery?.event_id ? [item.delivery] : []);
  const delivery = deliveries.find(d => d.event_id === editing.eventId) ?? deliveries[0];
  const eventId = textField(delivery, "event_id", "");
  const eventState = textField(delivery, "status", "not_prepared");
  return <section className={s.detail} aria-busy={loading}>
    <div className={s.detailHead}><div><p className={s.kicker}>需求版本 {item.version} · 工作修订 {item.revision}</p><h2>{titleOf(item)}</h2></div><span className={s.badge}>{label(item.status)}</span></div>
    <div className={s.facts}><div><small>任务负责人</small><b>{item.assignee ?? "待分派"}</b></div><div><small>设计审查</small><b>{item.status === "approved" ? "已批准指定版本" : "尚未批准"}</b></div><div><small>结果回传</small><b>{label(textField(item.delivery, "status", "not_prepared"), deliveryStatus)}</b></div></div>
    <section className={s.section}><h3>原始要求与完整设计提示词</h3><SourceRequirements binding={item.source_binding} /></section>
    {item.source_receipt && <details className={s.section}><summary>来源版本与技术接收凭据</summary><RecordView value={item.source_receipt} /></details>}
    {item.manager_note && <section className={s.section}><h3>负责人意见</h3><p className="recordText">{item.manager_note}</p></section>}
    {item.project_id && <p className={s.section}><Link className={s.primaryLink} href={`/projects/${item.project_id}?handoff=${encodeURIComponent(item.id)}`}>{user.role === "manager" ? "查看设计工作区" : "打开任务，继续设计"} →</Link></p>}
    {item.submitted_version_id && <section className={s.section}><h3>提交审查的方案</h3>{imageError ? <div className={s.reviewImage} role="status"><p>方案图片暂不可读取，原提交记录仍保留。</p><button onClick={() => setImageError(false)}>重试读取图片</button></div> : <img className={s.reviewImage} src={agentApi.image(item.submitted_version_id, "version")} alt="指定提交版本的鞋服方案" width={650} height={650} onError={() => setImageError(true)} />}<p>版本 {item.submitted_version_id}</p>{item.project_id && <Link href={`/designs/${encodeURIComponent(item.submitted_version_id)}?project=${item.project_id}`}>查看完整方案与检查记录 →</Link>}</section>}
    {error && <p className={s.error} role="alert">{error}</p>}{notice && <p className={s.notice} role="status">{notice}</p>}
    {stale && <div className={s.conflict} role="alert"><h3>任务已有新修订</h3><p>你的编辑基于修订 {editing.base}，当前为 {item.revision}。核对上方当前记录后，可保留意见并以新修订继续。</p><button disabled={busy || !!pending} onClick={() => patch({ base: item.revision })}>已核对，保留编辑并采用当前修订</button></div>}
    {pending && <section className={s.conflict}><h3>原操作待核对</h3><p>操作编号 {pending.id}。编辑已暂存并锁定；先查询原记录，再恢复编辑。刷新不会自动重发。</p><div className={s.headerActions}><button disabled={busy} onClick={() => void lookup()}>查询原操作</button><button disabled={busy || !canReplay(pending, user)} onClick={() => void run(pending)}>原请求重试</button><button disabled={busy || !operation} onClick={() => { remember(null); setOperation(null); setDraft(id, null); onChanged(); }}>已核对记录，结束恢复</button></div>{rejected && <button disabled={busy} onClick={() => { remember(null); setOperation(null); setRejected(false); onChanged(); }}>原请求已被拒绝，保留编辑并重新核对</button>}{!canReplay(pending, user) && <p>原操作属于之前的会话，当前只能查询。</p>}{operation && <details open><summary>原操作快照</summary><RecordView value={operation} /></details>}</section>}
    <section className={s.actions}><h3>{user.role === "manager" ? "处理这项需求" : "提交我的方案"}</h3>
      {user.role === "manager" && <><label>处理意见<textarea value={editing.note} onChange={e => patch({ note: e.target.value })} maxLength={2000} rows={3} placeholder="记录可行性判断、需要澄清的问题或具体修改意见…" disabled={busy || !!pending} /></label>
        {(allows("accept") || allows("return")) && <div className={s.headerActions}>{allows("accept") && <button className={s.primary} disabled={blocked} onClick={() => act("decision", { action: "accept", note: editing.note })}>确认可执行，接收需求</button>}{allows("return") && <button disabled={blocked || !editing.note.trim()} onClick={() => act("decision", { action: "return", note: editing.note })}>退回产品澄清</button>}</div>}
        {allows("assign") && !item.assignee && <><div className={s.facts}><label>设计人员<select value={editing.assignee} onChange={e => patch({ assignee: e.target.value })} disabled={busy || !!pending}><option value="">选择设计人员</option>{staff.filter(p => p.active).map(p => <option key={p.username} value={p.username}>{p.display_name} · {p.username}</option>)}</select></label><label>本次优先级<select value={editing.priority} onChange={e => patch({ priority: e.target.value })} disabled={busy || !!pending}><option value="">沿用已批准需求</option>{["P0", "P1", "P2", "P3"].map(p => <option key={p}>{p}</option>)}</select></label><label>约定日期<input type="date" value={editing.due} onChange={e => patch({ due: e.target.value })} disabled={busy || !!pending} /></label></div><button className={s.primary} disabled={blocked || !editing.assignee || (!!item.assignee && !editing.note.trim())} onClick={() => act("decision", { action: "assign", assignee: editing.assignee, note: editing.note, ...(editing.priority ? { priority: editing.priority } : {}), ...(editing.due ? { due_date: editing.due } : {}) })}>确认分派</button></>}
        {(allows("approve") || allows("revise")) && <div className={s.headerActions}>{allows("approve") && <button className={s.primary} disabled={blocked || !item.submitted_version_id || !editing.note.trim()} onClick={() => act("review", { action: "approve", submitted_version_id: item.submitted_version_id, note: editing.note })}>批准此提交版本</button>}{allows("revise") && <button disabled={blocked || !editing.note.trim()} onClick={() => act("review", { action: "revise", submitted_version_id: item.submitted_version_id, note: editing.note })}>退回设计人员修改</button>}</div>}
        {!!deliveries.length && <section className={s.section}><h3>交接回传</h3><label>选择待核对事件<select value={eventId} onChange={e => patch({ eventId: e.target.value })} disabled={busy || !!pending}>{deliveries.map(d => <option key={textField(d, "event_id")} value={textField(d, "event_id")}>{textField(d, "kind", "交接事件")} · {label(textField(d, "status"), deliveryStatus)}</option>)}</select></label><p className={s.subtle}>技术收件、澄清和设计审查分别回传，发送不会改写业务批准。</p><div className={s.headerActions}><button disabled={blocked || !allows("send") || !eventId || !["prepared", "failed_before_send"].includes(eventState)} onClick={() => act("delivery", { action: "send", event_id: eventId })}>发送所选交接记录</button><button disabled={blocked || !allows("reconcile") || !eventId || !["unconfirmed", "sending"].includes(eventState)} onClick={() => act("delivery", { action: "reconcile", event_id: eventId })}>查询所选事件的接收结果</button></div></section>}
      </>}
      {user.role === "designer" && allows("submit") && <><label>选择已确认的方案版本<select value={editing.versionId} onChange={e => patch({ versionId: e.target.value })} disabled={busy || !!pending}><option value="">选择本次提交版本</option>{versions.map(v => <option key={v.id} value={v.id}>{v.style_direction?.name ?? "已确认方案"} · {v.id}</option>)}</select></label><p className={s.subtle}>此操作冻结指定版本供负责人审查，不代表设计已获批准。</p><button className={s.primary} disabled={blocked || !editing.versionId} onClick={() => act("submit", { version_id: editing.versionId })}>提交此版本审查</button></>}
      {!item.allowed_actions.length && <p className={s.subtle}>当前阶段没有可执行动作。你可以查看要求、历史和已有方案。</p>}
    </section>
    {(item.submission || item.review || item.delivery || (item.audit || item.history)) && <details className={s.history}><summary>提交、审查与传输记录</summary>{item.submission && <><h3>员工提交</h3><RecordView value={item.submission} /></>}{item.review && <><h3>管理审查</h3><RecordView value={item.review} /></>}{item.delivery && <><h3>回传状态</h3><RecordView value={item.delivery} /></>}{(item.history || item.audit) && <><h3>操作历史</h3><RecordView value={item.history ?? item.audit} /></>}</details>}
  </section>;
}
