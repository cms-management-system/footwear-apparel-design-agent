"use client";
import { designContextKey } from "@/lib/team-api";
import { appPath } from "@/lib/app-path";
import { useEffect, useRef, useState } from "react";
import { agentApi, type Capabilities, type SpecRecord, type Version } from "@/lib/agent-api";
import styles from "./design.module.css";

export default function VersionActions({ version, title, onPromptRevision, current, projectId, caps, hasModifiedVersion, selected, confirmableSpecId, awaitingTaskId, onReveal, locked, readOnly = false, onAction, onDirtyChange }: {
  onPromptRevision?: () => void; version: Version; title: string; current?: SpecRecord; projectId: number; caps: Capabilities | null; hasModifiedVersion: boolean; selected: boolean; locked: boolean; readOnly?: boolean; confirmableSpecId?: string; awaitingTaskId?: string;
  onReveal: () => void;
  onAction: (fn: () => Promise<unknown>) => Promise<boolean>;
  onDirtyChange: (id: string, dirty: boolean) => void;
}) {
  const revision = current?.spec.base_version_id === version.id ? current : undefined;
  const [region, setRegion] = useState("");
  const [feedback, setFeedback] = useState("");
  const [editing, setEditing] = useState(false);
  const [editBaseSpecId, setEditBaseSpecId] = useState<string | null>(null);
  const baseChanged = !!editBaseSpecId && editBaseSpecId !== current?.id;
  const editTrigger = useRef<HTMLButtonElement>(null);
  const firstField = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const actionLocked = locked || readOnly;
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { onDirtyChange(version.id, !!(region || feedback)); }, [region, feedback, version.id, onDirtyChange]);
  useEffect(() => { if (editing) firstField.current?.focus(); }, [editing]);
  useEffect(() => {
    if (!editing) return;
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setEditing(false); editTrigger.current?.focus({ preventScroll: true }); }
      if (event.key === "Tab") {
        const elements = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]') ?? []);
        const first = elements[0], last = elements.at(-1);
        if (!first) { event.preventDefault(); dialog.current?.focus(); }
        else if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, [editing]);
  function closeEditor() { setEditing(false); editTrigger.current?.focus({ preventScroll: true }); }
  async function openEditor() {
    if (readOnly) return;
    if (onPromptRevision) { onPromptRevision(); return; }
    if (awaitingTaskId) {
      const stopped = await onAction(() => agentApi.control(awaitingTaskId, "cancel"));
      if (!stopped) { setError("当前对话未能结束，请重试。"); return; }
    }
    if (!editBaseSpecId || (!region && !feedback)) setEditBaseSpecId(current?.id ?? null);
    setEditing(true);
  }
  async function confirmThis() {
    if (readOnly) return;
    if (awaitingTaskId) {
      const stopped = await onAction(() => agentApi.control(awaitingTaskId, "cancel"));
      if (!stopped) { setError("当前对话未能结束，请重试。"); return; }
    }
    run(() => agentApi.confirmVersion(version.id), () => setNotice(`${title}已选定。`));
  }
  async function recheckThis() {
    if (readOnly || !caps?.understand) return;
    const storage = `${designContextKey()}:design-recheck:${projectId}:${version.id}`;
    let key = sessionStorage.getItem(storage);
    if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storage, key); }
    await agentApi.recheckVersion(version.id, key);
    sessionStorage.removeItem(storage);
    setNotice("正在检查已保存的图片，图片不会重新生成。");
  }
  const run = (fn: () => Promise<unknown>, success?: () => void) => void onAction(async () => { setError(""); setNotice(""); await fn(); }).then(ok => { if (ok) success?.(); else setError("操作未完成，请检查提示后重试。"); });
  async function generateRevision() {
    if (readOnly || !revision || !caps?.design) return;
    const storage = `${designContextKey()}:design-revision:${projectId}:${revision.id}`;
    let key = sessionStorage.getItem(storage);
    if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storage, key); }
    await agentApi.submit(projectId, revision.id, "design", key, 1);
    sessionStorage.removeItem(storage);
    setNotice("正在以这张图为底图生成修改版，结果会显示在本方案下方。");
  }
  return <section className={styles.versionActions} aria-label={`${title}的操作`} onFocusCapture={onReveal} onPointerDown={onReveal}>
    {selected ? <div className={styles.chosenActions}>
      <div><strong>已选定这款</strong><p>修改版确认之前，原版会保持选定。</p></div>
      <div className={styles.actions}><a className={styles.pageLink} href={appPath(`/designs/${version.id}?project=${projectId}`)}>查看这张图的方案页 →</a><button ref={editTrigger} disabled={readOnly || (actionLocked && !awaitingTaskId) || !current} onClick={() => void openEditor()}>{awaitingTaskId ? "结束当前对话并修改这款" : "继续修改这款"}</button></div>
    </div> : <>
      <h3>确认或修改此方案</h3>
      {!version.review && <p className={styles.muted}>{actionLocked ? "正在处理当前任务，检查完成后才能确认。" : "这张图片的检查尚未完成，请先重新检查已有图片。"}</p>}
      <div className={styles.actions}>
        {version.execution_mode !== "image_only" && (!version.review || version.review.goal.status === "unknown") && <button disabled={actionLocked || !caps?.understand || version.spec_id !== current?.id || ["confirmed", "superseded"].includes(version.status)} onClick={() => run(recheckThis)}>重新检查已有图片</button>}
        <button className={styles.primary} disabled={readOnly || (actionLocked && !awaitingTaskId) || !version.review || ![current?.id, confirmableSpecId].includes(version.spec_id) || ["confirmed", "superseded"].includes(version.status)} onClick={() => void confirmThis()}>{awaitingTaskId ? "结束当前对话并确认这款" : "确认这款"}</button>
        {!hasModifiedVersion && <button ref={editTrigger} disabled={readOnly || (actionLocked && !awaitingTaskId) || !current} onClick={() => void openEditor()}>{awaitingTaskId ? "结束当前对话并修改这款" : "修改这款"}</button>}
        <a href={agentApi.delivery(version.id)}>下载图片与要求单</a>
      </div>
    </>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status" className={styles.notice}>{notice.startsWith("正在检查") && !locked ? (version.review ? "检查已完成，请查看逐项结果。" : "检查暂未完成，请查看任务提示后重试。") : notice}</p>}
    {readOnly && <p className={styles.muted}>此方案只读；员工选定不等于管理者审查批准。</p>}
    {!readOnly && locked && !awaitingTaskId && <p className={styles.muted}>当前任务进行中，操作暂不可用。</p>}
    {version.review?.checks.filter(c => c.status !== "pass").map(c => <div className={styles.check} key={c.constraint_id}><p>{c.evidence}</p>{version.spec_id === current?.id && current.spec.constraints.find(x => x.id === c.constraint_id)?.verification === "visual" && <button disabled={actionLocked || ["confirmed", "superseded"].includes(version.status)} onClick={() => { const evidence = window.prompt("请具体指出图片的哪个部位证明这一条已经符合要求（至少 10 个字）。原模型判断仍会保留。"); if (evidence) run(() => agentApi.correctCheck(version.id, c.constraint_id, evidence), () => setNotice("已记录你的视觉判断依据。")); }}>纠正这条视觉判断</button>}</div>)}
    {revision && !hasModifiedVersion && <section aria-label={`${title}的修改要求`} className={styles.revisionStep}>
      <h3>修改进行中</h3>
      <p>修改{revision.spec.edit_region}：{revision.revision_feedback?.text ?? revision.spec.constraints.filter(c => c.kind === "may_change" && c.region === revision.spec.edit_region).at(-1)?.text}</p>
      <p className={styles.muted}>以这张图为底图，其余部位保持不变。{selected && "原版仍是已选方案。"}</p>
      {revision.spec.conflicts.map((conflict, i) => <p className={styles.error} key={i}>{conflict}</p>)}
      <div className={styles.actions}>{revision.status !== "confirmed" ? <button disabled={actionLocked || !!revision.spec.conflicts.length} onClick={() => run(() => agentApi.confirmSpec(revision.id), () => setNotice("修改要求已确认，可以生成修改版。"))}>确认修改要求</button> : <button className={styles.primary} disabled={actionLocked || !caps?.design} onClick={() => run(generateRevision)}>生成修改版</button>}</div>
    </section>}
    {selected && hasModifiedVersion && <p className={styles.muted}>修改版已生成。确认新版后才会替换当前选定的原版。</p>}
    {editing && !readOnly && <div className={styles.editOverlay}>
      <div ref={dialog} tabIndex={-1} className={styles.editDialog} role="dialog" aria-modal="true" aria-labelledby={`edit-${version.id}`}>
        <form onSubmit={event => { event.preventDefault(); if (actionLocked || !current || !editBaseSpecId || baseChanged || !region.trim() || !feedback.trim()) return; run(() => agentApi.revise(version.id, feedback.trim(), region.trim(), editBaseSpecId), () => { setRegion(""); setFeedback(""); setEditBaseSpecId(null); closeEditor(); setNotice("修改要求已保存。确认要求后即可生成修改版。"); }); }}>
          <h2 id={`edit-${version.id}`}>修改{title}</h2>
          <p>以当前图片为底图，只改指定部位，其他设计保持不变。收起后保留未保存内容。</p>
          {error && <p role="alert" className={styles.error}>{error}</p>}
          {baseChanged && <div role="alert" className={styles.error}><p>设计要求版本已变化。修改内容仍保留，请核对最新要求后再保存。</p><button type="button" disabled={actionLocked || !current} onClick={() => setEditBaseSpecId(current?.id ?? null)}>已核对，使用当前要求版本</button></div>}
          <label>只修改哪个部位？<input ref={firstField} required value={region} maxLength={200} disabled={actionLocked} onChange={event => setRegion(event.target.value)} placeholder="例如：领口、腰线、颜色" /></label>
          <label>希望怎么修改？<textarea required value={feedback} maxLength={2000} disabled={actionLocked} onChange={event => setFeedback(event.target.value)} placeholder="具体描述你想看到的变化" /></label>
          <div className={styles.editDialogActions}><button type="button" disabled={actionLocked} onClick={closeEditor}>取消</button><button className={styles.primary} disabled={actionLocked || baseChanged || !current || !region.trim() || !feedback.trim()}>保存修改要求</button></div>
        </form>
      </div>
    </div>}
  </section>;
}
