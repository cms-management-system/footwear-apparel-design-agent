"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { agentApi, activeTask, statusLabel, type Workspace, type Capabilities } from "@/lib/agent-api";
import { storageOwner, errorText, type TeamUser } from "@/lib/team-api";
import { pendingEngineAction } from "@/lib/engine-api";
import { creationUnavailableMessage } from "@/lib/creation-copy";
import { workspaceApi, type MessageInput, type SelectedContext } from "@/lib/workspace-api";
import EngineRecovery from "@/components/team/EngineRecovery";
import { useSendKey } from "./useSendKey";
import CreationRuns, { runInProgress } from "./CreationRuns";
import ExecutionDraft from "./ExecutionDraft";
import s from "./workspace.module.css";
export default function ProjectConversation({ workspace, user, selected, onSelect, editable, caps, refresh, onDirty }: { workspace: Workspace; user: TeamUser; selected: SelectedContext | null; onSelect: (context: SelectedContext | null) => void; editable: boolean; caps: Capabilities | null; refresh: () => Promise<unknown>; onDirty: (dirty: boolean) => void }) {
  const [text, setText] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [draftOpen, setDraftOpen] = useState(false); const [draftDirty, setDraftDirty] = useState(false);
  const key = `design-message-draft:${storageOwner(user)}:${workspace.project.id}`;
  const alive = useRef(true); const lock = useRef(false); const frozen = useRef<{ signature: string; input: MessageInput } | null>(null); const bottom = useRef<HTMLDivElement>(null); const history = useRef<HTMLDivElement>(null); const following = useRef(true);
  useEffect(() => { alive.current = true; try { setText(sessionStorage.getItem(key) ?? ""); const pending = pendingEngineAction(workspace.project.id); if (pending?.path.endsWith("/design-messages") && pending.body) { const input = JSON.parse(pending.body) as MessageInput; frozen.current = { input, signature: JSON.stringify({ text: input.text, references: input.references, expected_prompt_id: input.expected_prompt_id, expected_spec_id: input.expected_spec_id, selected_context: input.selected_context, authorized: input.authorized, intent: input.intent, output_kind: input.output_kind }) }; } } catch { /* Keep in-memory editing available. */ } return () => { alive.current = false; }; }, [key, workspace.project.id]);
  useEffect(() => { onDirty(!!text.trim() || draftDirty); }, [text, draftDirty, onDirty]);
  const runs = workspace.creation_runs ?? [];
  const independent = workspace.project_context?.source_mode === "independent";
  const direct = caps?.direct_creation;
  const ongoing = runs.filter(run => ["queued", "running"].includes(run.status)).at(-1);
  const stageMessages = { text: "正在理解你的设计想法…", freeze: "正在整理设计方案…", image: "正在生成设计图片…", canvas: "正在将图片放入画布…", done: "正在完成本次创作…" };
  const introMessage = ongoing ? ongoing.status === "queued" ? "本次创作已排队，请稍候。" : stageMessages[ongoing.stage]
    : runs.some(run => run.status === "unknown") ? "原请求结果尚待核对，当前不会重新发送。"
    : direct ? direct.available ? "说出设计要求，Agent将整理方案并生成图片" : direct.text.available ? "当前可讨论设计；图片生成暂不可用" : creationUnavailableMessage(direct.reason)
    : caps ? caps.understand ? "对话任务以服务端状态为准" : "对话模型未启用 · 消息可保存，执行稿由你明确整理" : "正在核对对话能力";
  const keys = useSendKey(() => void send());
  const runStatus = runs.at(-1)?.status; const runStage = runs.at(-1)?.stage;
  const liveText = workspace.tasks.at(-1)?.live_text;
  useEffect(() => { if (following.current) bottom.current?.scrollIntoView?.({ block: "end", behavior: "auto" }); }, [workspace.messages?.length, liveText, runStatus, runStage]);
  const dirtyCallback = useCallback((value: boolean) => setDraftDirty(value), []);
  const task = workspace.tasks.filter(activeTask).at(-1) ?? workspace.tasks.at(-1); const working = workspace.tasks.some(activeTask) || runs.some(runInProgress);
  async function send(event?: FormEvent) {
    event?.preventDefault(); if (!editable || lock.current || working || !text.trim() || text.length > 4000) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    const value = { text, references: [] as [], expected_prompt_id: workspace.current_prompt_id ?? null, expected_spec_id: workspace.head.spec_id ?? null, selected_context: selected, authorized: !!direct?.text.available && direct.remaining.text_calls > 0, intent: "auto" as const, output_kind: null };
    const signature = JSON.stringify(value);
    if (frozen.current?.signature !== signature) frozen.current = { signature, input: { ...value, idempotency_key: crypto.randomUUID() } };
    try {
      const result = await workspaceApi.message(workspace.project.id, frozen.current.input);
      if (!alive.current) return; setText(""); sessionStorage.removeItem(key); frozen.current = null; following.current = true;
      setNotice(result.run_id ? "想法已保存，创作进度将在这里更新。" : result.reply_state === "unavailable" ? "消息已保存，当前未启用对话模型。可手动整理执行稿。" : result.task_id ? "消息已保存，原任务正在处理。" : "消息已保存。请核对项目历史。");
      try { await refresh(); } catch { if (alive.current) setNotice("消息已保存，显示暂未同步。请读取原记录，不要重复发送。"); }
    } catch (cause) { if (alive.current) setError(errorText(cause)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  const selectedVersion = workspace.versions.find(version => version.id === selected?.version_id);
  async function chooseVersion() {
    if (!selectedVersion || !editable || !workspace.project_context?.allowed_actions.includes("select_version") || lock.current || working) return;
    lock.current = true; setBusy(true); setError("");
    try { await agentApi.confirmVersion(selectedVersion.id); if (alive.current) { setNotice(`版本 ${selectedVersion.id} 已人工选定，负责人审查仍需单独完成。`); await refresh(); } }
    catch (cause) { if (alive.current) setError(errorText(cause)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  return <aside className={s.conversation} aria-label="项目持续对话"><header className={s.conversationHeader}><div><span className={s.eyebrow}>DESIGN PARTNER</span><h2>一起，把想法展开。</h2></div><button onClick={() => setDraftOpen(true)}>{independent ? "设计稿详情" : editable ? "整理执行稿" : "查看执行稿"}</button></header>
    <div ref={history} className={s.messageHistory} onScroll={() => { const el = history.current; if (el) following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }}>
      <div className={s.conversationIntro}><span className={s.partnerMark} aria-hidden="true">✳</span><p>{editable ? "说说鞋服的廓形、配色和细节。选中画布中的图片，可以基于它继续讨论。" : "这里保留这个项目的真实消息、候选稿和图片版本。"}</p><small>{introMessage}</small></div>
      {workspace.execution_brief_state === "needs_confirmation" && (!independent || !direct) && <p className={s.notice}>最新讨论待整理确认，旧执行稿暂不可生成。</p>}{workspace.execution_brief_state === "inconsistent" && <p className={s.warning}>{editable ? "执行稿与确认规格绑定待核对，请整理并确认当前候选。" : "原执行稿存在历史绑定不一致，已有图片和审查记录仍可查看。"}</p>}
      <EngineRecovery projectId={workspace.project.id} onResolved={refresh} />
      {(workspace.messages ?? []).map(message => <article key={message.id} className={message.role === "user" ? s.userMessage : message.role === "system" ? s.systemMessage : s.assistantMessage}><small>{message.role === "user" ? "你" : message.role === "system" ? "系统通知" : "设计助手"}</small>{message.selected_context && <div className={s.historyContext}>{message.selected_context.version_id ? `版本 ${message.selected_context.version_id}` : `素材 ${message.selected_context.asset_id}`}</div>}<p>{message.text}</p></article>)}
      <CreationRuns runs={runs} projectId={workspace.project.id} onSelect={onSelect} refresh={refresh} onError={cause => setError(errorText(cause))} />
      {task && !runs.some(run => run.text_task_id === task.id || run.image_task_id === task.id) && <div className={s.taskCard}><small>原任务 · {task.id}</small><p>{statusLabel(task.status)}{task.outcome === "unknown" ? " · 结果待核对，不重发" : ""}</p>{task.live_text && activeTask(task) && <p className={s.fullText}>{task.live_text}</p>}{task.error?.message && <p className={s.error}>{task.error.message}</p>}<button onClick={() => void refresh().catch(cause => setError(errorText(cause)))}>读取原任务进度</button></div>}
      {!!workspace.briefs?.length && <div className={s.briefSummary}><p>{workspace.briefs.length} 份候选稿保存在项目中</p><button onClick={() => setDraftOpen(true)}>查看并核对候选稿 →</button></div>}
      {notice && <p role="status" className={s.notice}>{notice}</p>}{error && <p role="alert" className={s.error}>{error}</p>}<div ref={bottom} />
    </div>
    <div className={s.composerWrap}>{selected && <div className={s.selectedContext}><img src={agentApi.image(selected.version_id ?? selected.asset_id!, selected.version_id ? "version" : "asset")} alt="当前选中图片上下文" width={44} height={44} /><div><b>{selected.version_id ? "基于选中版本讨论" : "讨论参考素材"}</b><small>{selected.version_id ?? selected.asset_id}</small></div><button aria-label="移除选中图片上下文" onClick={() => onSelect(null)}>×</button></div>}
      {editable ? <form onSubmit={event => void send(event)} className={s.messageComposer}><label className={s.srOnly} htmlFor="project-message">告诉设计助手你的想法</label><textarea id="project-message" {...keys} value={text} maxLength={4000} rows={3} disabled={busy} placeholder="描述新想法，或说说这张图想怎么改…" onChange={e => { setText(e.target.value); setNotice(""); try { sessionStorage.setItem(key, e.target.value); } catch { /* Keep the current draft. */ } }} /><div className={s.composerBottom}><span>{busy ? "正在保存消息…" : working ? "原任务处理中" : "Enter 发送 · Shift + Enter 换行"}</span><button className={s.sendButton} type="submit" aria-label="发送项目消息" disabled={busy || working || !text.trim()}>↑</button></div></form> : <p className={s.readOnlyNote}>当前项目只读。分派和负责人审查请进入任务管理。</p>}
      {selectedVersion && <div className={s.selectedVersionActions}><Link href={`/designs/${encodeURIComponent(selectedVersion.id)}?project=${workspace.project.id}`}>查看版本与下载 →</Link>{selectedVersion.status === "confirmed" ? <span>已人工选定 · 审查另行记录</span> : editable && workspace.project_context?.allowed_actions.includes("select_version") ? <button disabled={busy || working} onClick={() => void chooseVersion()}>选定此设计方案</button> : <span>只读查看版本</span>}</div>}
      <p className={s.composerHint}>{independent ? "直接说出想法或改图要求，实际回复与图片保存在项目中。" : "上游设计遵循来源约束与确认、人审流程。"}</p>
    </div>{draftOpen && <ExecutionDraft workspace={workspace} user={user} selected={selected} editable={editable} onClose={() => setDraftOpen(false)} onRefresh={refresh} onDirty={dirtyCallback} />}
  </aside>;
}
