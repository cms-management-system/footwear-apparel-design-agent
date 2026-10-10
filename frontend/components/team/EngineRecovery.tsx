"use client";
import { useEffect, useState } from "react";
import { errorText } from "@/lib/team-api";
import { ENGINE_RECOVERY_EVENT, canRetryEngine, pendingEngineAction, queryEngineAction, retryEngineAction, type EngineAction } from "@/lib/engine-api";
import s from "@/app/handoffs/handoff.module.css";

export default function EngineRecovery({ projectId, onResolved }: { projectId: number; onResolved: () => Promise<unknown> }) {
  const [pending, setPending] = useState<EngineAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const update = () => setPending(pendingEngineAction(projectId)); update();
    window.addEventListener(ENGINE_RECOVERY_EVENT, update);
    return () => window.removeEventListener(ENGINE_RECOVERY_EVENT, update);
  }, [projectId]);
  async function recover(action: "query" | "retry") {
    if (busy) return; setBusy(true); setError("");
    try { await (action === "query" ? queryEngineAction(projectId) : retryEngineAction(projectId)); await onResolved(); }
    catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  }
  if (!pending) return null;
  return <section className={s.conflict} aria-label="设计操作恢复" aria-busy={busy}><h3>正在核对原设计操作</h3><p>原操作 {pending.id} · 基准修订 {pending.revision}。新编辑仍保留，先查询原记录；刷新不会自动重发。</p><div className={s.headerActions}><button disabled={busy} onClick={() => void recover("query")}>查询原设计操作</button><button disabled={busy || !canRetryEngine(pending)} onClick={() => void recover("retry")}>重试原设计操作</button></div>{!canRetryEngine(pending) && <p>原会话已变化或原文件不在内存中。当前可查询记录；同一会话中重新选择原文件后，可从原上传入口重试。</p>}{error && <p role="alert" className={s.error}>{error}</p>}</section>;
}
