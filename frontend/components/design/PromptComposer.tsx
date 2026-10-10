"use client";
import { useEffect, useRef, useState } from "react";
import { agentApi, activeTask, type Capabilities, type Workspace } from "@/lib/agent-api";
import { errorText } from "@/lib/team-api";
import styles from "./design.module.css";

function text(value: unknown) { return typeof value === "string" ? value : ""; }
function sourcePrompt(workspace: Workspace): Record<string, unknown> { const value = workspace.source_binding?.prompt; return value && typeof value === "object" ? value as Record<string, unknown> : {}; }
export default function PromptComposer({ workspace, revisionVersionId, readOnly, onSaved, onDirtyChange }: { workspace: Workspace; revisionVersionId?: string | null; readOnly: boolean; onSaved: () => Promise<unknown>; onDirtyChange: (dirty: boolean) => void }) {
  const current = workspace.prompts?.find(prompt => prompt.id === workspace.current_prompt_id);
  const source = sourcePrompt(workspace);
  const [prompt, setPrompt] = useState(current?.positive_prompt ?? text(source.positive_prompt));
  const [avoid, setAvoid] = useState((current?.avoid_items ?? (Array.isArray(source.avoid_items) ? source.avoid_items.filter(item => typeof item === "string") as string[] : [])).join("\n"));
  const [kind, setKind] = useState<"effect_image" | "design_draft">(current?.output_kind ?? "effect_image");
  const [base, setBase] = useState(current?.base_version_id ?? "");
  const [region, setRegion] = useState(current?.edit_region ?? "");
  const [expected, setExpected] = useState(current?.id ?? null);
  const [dirty, setDirty] = useState(false);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alive = useRef(true); const lock = useRef(false); const lastRevisionTarget = useRef<string | null>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { let current = true; agentApi.capabilities(workspace.project.id).then(value => { if (current) setCaps(value); }).catch(() => { if (current) setCaps(null); }); return () => { current = false; }; }, [workspace.project.id, workspace.revision]);
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (dirty || busy || !current || expected === current.id) return;
    setPrompt(current.positive_prompt); setAvoid(current.avoid_items.join("\n")); setKind(current.output_kind); setBase(current.base_version_id ?? ""); setRegion(current.edit_region); setExpected(current.id);
  }, [current, dirty, busy, expected]);
  useEffect(() => { if (!revisionVersionId) { lastRevisionTarget.current = null; return; } if (lastRevisionTarget.current !== revisionVersionId && workspace.versions.some(version => version.id === revisionVersionId)) { lastRevisionTarget.current = revisionVersionId; setBase(revisionVersionId); setRegion(""); setDirty(true); } }, [revisionVersionId, workspace.versions]);
  const stale = (current?.id ?? null) !== expected;
  const task = workspace.tasks.filter(item => item.mode === "image_only").at(-1);
  const working = workspace.tasks.some(activeTask);
  const unknown = task?.status === "interrupted" && task.outcome === "unknown";
  const generation = workspace.image_only_execution ?? caps?.image_only_execution;
  const reasonLabel: Record<string, string> = { IMAGE_AUTHORIZATION_REQUIRED: "当前项目尚未获得图片生成授权。可以保存设计描述并查看已有方案。", IMAGE_QUOTA_EXHAUSTED: "本轮单张图片额度已使用。请查看原任务与已保存图片。", CAPABILITY_UNAVAILABLE: "图片服务尚未启用。文字与已有方案仍保留。" };
  const unavailable = readOnly || busy || working;
  function changed() { setDirty(true); setNotice(""); }
  async function save() {
    if (lock.current || unavailable || stale || !prompt.trim() || (base && !region.trim())) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const saved = await agentApi.savePrompt(workspace.project.id, { expected_prompt_id: expected, positive_prompt: prompt.trim(), avoid_items: avoid.split("\n").map(value => value.trim()).filter(Boolean), output_kind: kind, base_version_id: base || null, edit_region: region.trim() });
      if (!alive.current) return;
      setExpected(saved.prompt.id); setDirty(false); setNotice("执行提示词已保存。核对正文后，可以明确生成一张图。"); await onSaved();
    } catch (cause) { if (alive.current) setError(errorText(cause)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  async function generate() {
    if (lock.current || unavailable || dirty || stale || !current || !generation?.available || generation.remaining === 0 || unknown) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try { const result = await agentApi.textToImage(workspace.project.id, current.id); if (alive.current) { setNotice(`已受理一张图，任务 ${result.task_id}。请从当前任务查看进度，刷新不会重新生成。`); await onSaved(); } }
    catch (cause) { if (alive.current) setError(errorText(cause)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  return <section id="image-prompt-editor" className={styles.chatCard} aria-label="直接文生图设计"><span className={styles.eyebrow}>{workspace.project_context?.source_mode === "independent" ? "自主设计" : "上游定向设计"} · 自然语言创作</span><h2>描述设计，生成你的方案</h2><p className={styles.muted}>{workspace.project_context?.source_mode === "independent" ? "从你的创意开始，修改会形成新的提示词与图片版本。" : "执行稿单独保存，上游完整要求与硬约束继续保留。"}</p>
    <fieldset disabled={unavailable} className={styles.startFields}><label>本轮完整设计描述<textarea maxLength={10000} rows={8} value={prompt} onChange={e => { setPrompt(e.target.value); changed(); }} placeholder="用自然语言描述鞋服对象、廓形、配色和细节，也可以基于原图提出修改要求。" /></label><label>需要避免的部分（每行一项）<textarea rows={3} value={avoid} onChange={e => { setAvoid(e.target.value); changed(); }} /></label><label>画面类型<select value={kind} onChange={e => { setKind(e.target.value as typeof kind); changed(); }}><option value="effect_image">鞋服效果图</option><option value="design_draft">二维设计稿</option></select></label>{workspace.versions.length > 0 && <><label>本轮修改基于<select value={base} onChange={e => { setBase(e.target.value); changed(); }}><option value="">从当前文字开始新方案</option>{workspace.versions.map(version => <option key={version.id} value={version.id}>图片版本 {version.id}</option>)}</select></label>{base && <label>想修改的位置<input maxLength={200} value={region} onChange={e => { setRegion(e.target.value); changed(); }} placeholder="例如：领口、鞋面配色、裙摆" /></label>}</>}</fieldset>
    {stale && <p role="alert" className={styles.error}>执行提示词已有新版本，当前编辑仍保留。<button disabled={busy} onClick={() => setExpected(current?.id ?? null)}>已核对当前执行稿，以新版本继续</button></p>}
    {!readOnly && <div className={styles.actions}><button disabled={unavailable || stale || !prompt.trim() || (!!base && !region.trim()) || (!dirty && !!current)} onClick={() => void save()}>保存执行提示词</button><button className={styles.primary} disabled={unavailable || dirty || stale || !current || !generation?.available || generation.remaining === 0 || unknown} onClick={() => void generate()}>{busy ? "正在提交…" : "生成一张设计图"}</button></div>}
    {current && <p className={styles.muted}>执行提示词 {current.id} · {current.positive_prompt.length} 字 · 图像与该版本绑定</p>}
    {!generation?.available || generation.remaining === 0 ? <p role="status" className={styles.muted}>{(generation?.reason ? reasonLabel[generation.reason] ?? `图片能力待核对（${generation.reason}）` : "") || "当前项目的图片生成能力或配额尚未开放。文字和已有方案可继续查看。"}</p> : <p className={styles.muted}>本次明确生成一张图。效果图与二维设计稿为设计示意，完成后仍需人工核对。</p>}
    {task && <p role="status" className={styles.muted}>原任务 {task.id} · {task.status === "succeeded" ? "图片已保存" : task.status === "queued" ? "已排队" : task.status === "running" ? "正在生成" : task.status === "interrupted" ? "结果待核对" : task.status === "failed" ? "本次未完成" : "状态待核对"}{unknown ? "；请查询原任务，不会自动重新调用。" : ""} <button onClick={() => void onSaved().catch(cause => setError(errorText(cause)))}>读取原任务进度</button></p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}{notice && <p role="status" className={styles.muted}>{notice}</p>}
  </section>;
}
