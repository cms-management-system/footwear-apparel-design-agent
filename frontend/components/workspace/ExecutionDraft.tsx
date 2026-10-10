"use client";
import { useEffect, useRef, useState } from "react";
import { agentApi, activeTask, type Workspace } from "@/lib/agent-api";
import { readExecutionDraft, storeExecutionDraft } from "@/lib/workspace-drafts";
import { errorText, type TeamUser } from "@/lib/team-api";
import { workspaceApi, type Brief, type SelectedContext } from "@/lib/workspace-api";
import SourceRequirements from "@/components/team/SourceRequirements";
import s from "./workspace.module.css";
export default function ExecutionDraft({ workspace, user, selected, editable, onClose, onRefresh, onDirty }: { user: TeamUser; workspace: Workspace; selected: SelectedContext | null; editable: boolean; onClose: () => void; onRefresh: () => Promise<unknown>; onDirty: (dirty: boolean) => void }) {
  const current = workspace.prompts?.find(p => p.id === workspace.current_prompt_id);
  const latestUser = workspace.messages?.find(m => m.role === "user" && m.id === (workspace.latest_user_message_id ?? workspace.head.latest_user_message_id));
  const [text, setText] = useState(current?.positive_prompt ?? ""); const [avoid, setAvoid] = useState(current?.avoid_items.join("\n") ?? "");
  const [kind, setKind] = useState<"effect_image" | "design_draft">(current?.output_kind ?? "effect_image"); const [base, setBase] = useState(selected?.version_id ?? current?.base_version_id ?? ""); const [region, setRegion] = useState(selected?.version_id && selected.version_id !== current?.base_version_id ? "" : current?.edit_region ?? "");
  const [expectedLatest, setExpectedLatest] = useState(workspace.latest_user_message_id ?? workspace.head.latest_user_message_id ?? null);
  const [expectedPrompt, setExpectedPrompt] = useState(workspace.current_prompt_id ?? null); const [expectedSpec, setExpectedSpec] = useState(workspace.head.spec_id ?? null);
  const [hasLocalDraft, setHasLocalDraft] = useState(() => !!readExecutionDraft(user, workspace.project.id));
  const [brief, setBrief] = useState<Brief | null>(null); const [dirty, setDirty] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const dialog = useRef<HTMLDialogElement>(null); const alive = useRef(true); const lock = useRef(false);
  const latest = workspace.latest_user_message_id ?? workspace.head.latest_user_message_id ?? null;
  const stale = (!!brief && (brief.based_on_latest_message_id !== latest)) || expectedLatest !== latest || expectedPrompt !== (workspace.current_prompt_id ?? null) || expectedSpec !== (workspace.head.spec_id ?? null);
  const confirmed = brief?.status === "confirmed" || current?.source_brief_id === brief?.id;
  const unknown = workspace.tasks.some(task => task.outcome === "unknown" || task.status === "unknown");
  const execution = workspace.image_only_execution;
  const working = workspace.tasks.some(activeTask);
  const bindingValid = !!current && workspace.execution_brief_state === "ready" && workspace.specs.some(spec => spec.id === workspace.head.spec_id && spec.status === "confirmed");
  const canGenerate = editable && !!workspace.project_context?.allowed_actions.includes("generate_image") && !busy && !working && !unknown && !dirty && !!current && bindingValid && text === current.positive_prompt && avoid === current.avoid_items.join("\n") && base === (current.base_version_id ?? "") && region === current.edit_region && kind === current.output_kind && (!brief || confirmed) && !stale && workspace.execution_brief_state === "ready" && (current.confirmed_through_message_id ?? null) === latest && !!execution?.available && execution.remaining > 0;
  useEffect(() => { alive.current = true; const element = dialog.current; const previous = document.activeElement as HTMLElement | null; if (element?.showModal) element.showModal(); else element?.setAttribute("open", ""); return () => { alive.current = false; element?.close?.(); previous?.focus?.(); }; }, []);
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => { if (dirty) { storeExecutionDraft(user, workspace.project.id, { text, avoid, kind, base, region, expectedPrompt, expectedSpec, expectedLatest }); setHasLocalDraft(true); } }, [user, workspace.project.id, text, avoid, kind, base, region, expectedPrompt, expectedSpec, expectedLatest, dirty]);
  function restoreDraft() { const saved = readExecutionDraft(user, workspace.project.id); if (!saved) return; setText(saved.text); setAvoid(saved.avoid); setKind(saved.kind); setBase(saved.base); setRegion(saved.region); setExpectedPrompt(saved.expectedPrompt); setExpectedSpec(saved.expectedSpec); setExpectedLatest(saved.expectedLatest); setBrief(null); setDirty(true); setNotice("已恢复当前账号的本地执行稿编辑，请核对当前版本后再保存。"); }
  function change() { setDirty(true); setBrief(null); setNotice(""); }
  async function run(fn: () => Promise<void>) { if (lock.current || !editable || working) return; lock.current = true; setBusy(true); setError(""); try { await fn(); } catch (cause) { if (alive.current) setError(errorText(cause)); } finally { lock.current = false; if (alive.current) setBusy(false); } }
  async function candidate() {
    if (!workspace.project_context?.allowed_actions.includes("save_brief") || !text.trim() || stale || (base && !region.trim())) return;
    await run(async () => {
      const context = base ? { version_id: base, asset_id: selected?.version_id === base ? selected.asset_id : null } : selected?.asset_id ? { version_id: null, asset_id: selected.asset_id } : null;
      const result = await workspaceApi.brief(workspace.project.id, { expected_prompt_id: expectedPrompt, expected_spec_id: expectedSpec, message_id: latestUser?.created_by === user.username ? latestUser.id : null, selected_context: context, positive_prompt: text.trim(), avoid_items: avoid.split("\n").map(v => v.trim()).filter(Boolean), output_kind: kind, base_version_id: base || null, edit_region: region.trim() });
      if (!alive.current) return; setBrief(result.brief); setText(result.brief.positive_prompt); setAvoid(result.brief.avoid_items.join("\n")); setDirty(false); storeExecutionDraft(user, workspace.project.id, null); setHasLocalDraft(false); setNotice("人工候选稿已保存，尚未成为执行稿。"); await onRefresh();
    });
  }
  async function confirm() {
    if (!workspace.project_context?.allowed_actions.includes("save_prompt") || !brief || dirty || stale || confirmed) return;
    await run(async () => {
      const result = await workspaceApi.confirm(workspace.project.id, { candidate_id: brief.id, expected_prompt_id: expectedPrompt, expected_spec_id: expectedSpec, positive_prompt: brief.positive_prompt, avoid_items: brief.avoid_items, output_kind: brief.output_kind, base_version_id: brief.base_version_id, edit_region: brief.edit_region });
      if (!alive.current) return; setExpectedPrompt(result.prompt.id); setExpectedSpec(result.spec_id); setExpectedLatest(result.prompt.confirmed_through_message_id ?? null); setBrief({ ...brief, status: "confirmed" }); storeExecutionDraft(user, workspace.project.id, null); setHasLocalDraft(false); setNotice("执行稿已确认保存。图片生成与负责人审查仍是独立操作。"); await onRefresh();
    });
  }
  async function generate() { if (!canGenerate) return; await run(async () => { const result = await agentApi.textToImage(workspace.project.id, current!.id); if (alive.current) { setNotice(`原任务 ${result.task_id} 已受理；请查询进度，不重复生成。`); await onRefresh(); } }); }
  function choose(choosing: Brief) { if (dirty && !window.confirm("当前编辑尚未保存，改为查看这份候选稿？")) return; setBrief(choosing); setText(choosing.positive_prompt); setAvoid(choosing.avoid_items.join("\n")); setKind(choosing.output_kind); setBase(choosing.base_version_id ?? ""); setRegion(choosing.edit_region); setExpectedPrompt(choosing.based_on_prompt_id); setExpectedSpec(choosing.based_on_spec_id); setExpectedLatest(choosing.based_on_latest_message_id); setDirty(false); setNotice(""); }
  function close() { if (dirty && !window.confirm("当前执行稿编辑尚未保存，关闭编辑？")) return; onClose(); }
  return <dialog ref={dialog} className={s.draftDialog} aria-label="执行稿与候选确认" onCancel={e => { e.preventDefault(); close(); }}><div className={s.draftHeader}><div><p className={s.eyebrow}>执行稿 · 明确保存后再生成</p><h2>{editable ? "整理本轮设计" : "查看设计执行稿"}</h2></div><button aria-label="关闭执行稿" onClick={close}>×</button></div><div className={s.draftBody}>
    {hasLocalDraft && !dirty && <div className={s.notice}>此账号在当前项目有未提交的本地编辑。<button onClick={restoreDraft}>恢复本人执行稿草稿</button></div>}
    <p className={s.muted}>{brief ? `${brief.origin === "model" ? "模型" : "人工"}候选 · ${brief.id}` : "人工整理不会调用模型"} · {confirmed ? "已确认执行稿" : "尚未确认"}</p>
    {latestUser && <details open><summary>本轮最新讨论</summary><pre className={s.fullText}>{latestUser.text}</pre>{latestUser.selected_context && <p className={s.muted}>讨论上下文：{latestUser.selected_context.version_id ?? latestUser.selected_context.asset_id}</p>}</details>}
    {!!workspace.briefs?.length && <details><summary>已保存的候选稿（{workspace.briefs.length}）</summary><div className={s.briefList}>{workspace.briefs.map(b => <button key={b.id} onClick={() => choose(b)}>{b.origin === "model" ? "模型候选" : "人工候选"} · {b.status === "confirmed" ? "已确认" : "待确认"}<small>{b.positive_prompt.slice(0, 100)}</small></button>)}</div></details>}
    <fieldset disabled={!editable || busy || working} className={s.draftFields}><label>完整正向设计描述<textarea rows={8} maxLength={10000} value={text} onChange={e => { setText(e.target.value); change(); }} /></label><label>避免项（每行一项）<textarea rows={3} value={avoid} onChange={e => { setAvoid(e.target.value); change(); }} /></label><div className={s.fieldPair}><label>画面类型<select value={kind} onChange={e => { setKind(e.target.value as typeof kind); change(); }}><option value="effect_image">鞋服效果图</option><option value="design_draft">二维设计稿</option></select></label><label>修改基于<select value={base} onChange={e => { setBase(e.target.value); change(); }}><option value="">从文字开始新方案</option>{workspace.versions.map(v => <option key={v.id} value={v.id}>版本 {v.id}</option>)}</select></label></div>{base && <label>修改区域<input maxLength={200} value={region} onChange={e => { setRegion(e.target.value); change(); }} placeholder="例如：领口、鞋面、裙摆" /></label>}</fieldset>
    {current && <details><summary>与当前执行稿核对</summary><p className={s.muted}>对象：{current.design_object} · 当前执行稿 {current.id}</p><pre className={s.fullText}>{current.positive_prompt}</pre><p>{current.positive_prompt === text && current.avoid_items.join("\n") === avoid && current.base_version_id === (base || null) && current.edit_region === region && current.output_kind === kind ? "正文与当前执行稿一致" : "本轮编辑与当前执行稿不同，请核对全文、避免项和修改基准。"}</p></details>}
    {workspace.source_binding && <details><summary>完整来源与硬约束</summary><SourceRequirements binding={workspace.source_binding} /></details>}
    {stale && <div className={s.warning} role="alert">当前执行稿、规格或最新消息已变化，编辑内容仍保留。<button disabled={busy || !editable} onClick={() => { setExpectedPrompt(workspace.current_prompt_id ?? null); setExpectedSpec(workspace.head.spec_id ?? null); setExpectedLatest(latest); setBrief(null); setDirty(true); }}>已核对当前版本，重新保存人工候选</button></div>}
    {error && <p className={s.error} role="alert">{error}</p>}{notice && <p className={s.notice} role="status">{notice}</p>}
    <p className={s.muted}>{unknown ? "原任务结果未知，只查询原任务，不重新生成。" : !execution?.available || !execution.remaining ? "当前项目没有可用图片授权或额度。文字、候选稿与已有图片可继续查看。" : "本次可明确生成一张图，仍需人工核对。"}</p>{workspace.execution_brief_state === "needs_confirmation" && <p className={s.warning}>最新消息尚未确认进入执行稿。请整理候选并明确确认后再生成。</p>}{!bindingValid && current && <p className={s.muted}>{editable ? "执行稿与确认规格绑定待核对，请确认保存当前候选稿后再生成。" : "原执行稿存在历史绑定不一致，当前只读，不更改已审查结果。"}</p>}
  </div><footer className={s.draftFooter}>{editable && <><button disabled={busy || working || stale || !text.trim() || (!!base && !region.trim())} onClick={() => void candidate()}>保存人工候选稿</button><button className={s.primary} disabled={busy || working || !brief || dirty || stale || confirmed} onClick={() => void confirm()}>确认并保存执行稿</button><button disabled={!canGenerate} onClick={() => void generate()}>生成一张设计图</button></>}{!editable && <span className={s.muted}>只读查看 · 负责人审查在任务管理中完成</span>}</footer></dialog>;
}
