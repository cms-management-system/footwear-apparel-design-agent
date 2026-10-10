"use client";
import { useEffect, useRef, useState } from "react";
import { ApiError, errorText, type TeamUser } from "@/lib/team-api";
import { canReplay, executeAction, freezeAction, pendingKey, workflowApi, type FrozenAction } from "@/lib/design-workflow";
import s from "@/app/handoffs/handoff.module.css";

export default function SyncInbox({ user, onChanged }: { user: TeamUser; onChanged: () => void }) {
  const [pending, setPending] = useState<FrozenAction | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [rejected, setRejected] = useState(false);
  const lock = useRef(false);
  const mounted = useRef(true);
  const key = pendingKey(user, "_inbox_sync");
  useEffect(() => {
    mounted.current = true;
    try { const saved = JSON.parse(sessionStorage.getItem(key) ?? "null") as FrozenAction | null; if (saved?.path === "/design-handoffs/sync" && saved.method === "POST" && /^[A-Za-z0-9_-]{8,100}$/.test(saved.id) && saved.actor === user.username && saved.scope === user.scope_id && saved.instance === user.instance_id && typeof saved.body === "string" && typeof saved.context === "string") setPending(saved); } catch { /* Ignore invalid local recovery data. */ }
    return () => { mounted.current = false; };
  }, [key, user.username, user.scope_id, user.instance_id]);
  function remember(action: FrozenAction | null) { setPending(action); try { if (action) sessionStorage.setItem(key, JSON.stringify(action)); else sessionStorage.removeItem(key); } catch { /* Keep in-memory recovery. */ } }
  function received(result: Record<string, unknown>) { setCursor(typeof result.next_cursor === "string" ? result.next_cursor : null); remember(null); setNotice("本页已核验并记录。技术接收不会自动接单或分派。"); onChanged(); }
  async function run(action: FrozenAction) {
    if (lock.current) return; lock.current = true; setBusy(true); setError(""); setRejected(false);
    try { const result = await executeAction<Record<string, unknown>>(action, user); if (mounted.current) received(result); }
    catch (cause) { if (mounted.current) { setError(errorText(cause)); setRejected(cause instanceof ApiError && [400, 403, 404, 409, 410, 422, 503].includes(cause.status)); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  function sync(next = false) { if (lock.current || pending) return; try { const action = freezeAction(user, "/design-handoffs/sync", { limit: 20, ...(next && cursor ? { cursor } : {}) }); remember(action); void run(action); } catch (cause) { setError(errorText(cause)); } }
  async function query() {
    if (!pending || lock.current) return; lock.current = true; setBusy(true); setError("");
    try {
      const operation = await workflowApi.operation(pending.id);
      if (!mounted.current) return;
      if (typeof operation.status_code !== "number" || operation.status_code < 200 || operation.status_code >= 300 || !operation.result || typeof operation.result !== "object") {
        setError("原同步记录尚未确认成功。请求仍保留，请核对后再恢复。");
        setRejected(typeof operation.status_code === "number" && [400, 403, 404, 409, 410, 422, 503].includes(operation.status_code));
      } else received(operation.result as Record<string, unknown>);
    }
    catch (cause) { if (mounted.current) setError(cause instanceof ApiError && cause.status === 404 ? "尚未查到原同步记录，可在同一会话下重试原请求。" : errorText(cause)); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  return <section className={s.toolbar} aria-label="同步产品已批准需求"><div className={s.headerActions}><button className={s.primary} disabled={busy || !!pending || !user.auth_context_id} onClick={() => sync()}>{busy ? "正在核对…" : "同步产品已批准需求"}</button>{cursor && <button disabled={busy || !!pending} onClick={() => sync(true)}>继续同步下一页</button>}</div><p className={s.subtle}>只接收产品后台已批准的完整版本。</p>{error && <p role="alert" className={s.error}>{error}</p>}{notice && <p role="status" className={s.notice}>{notice}</p>}{pending && <div className={s.conflict}><p>原同步操作仍保留。请先查询记录，避免重复发送。</p><div className={s.headerActions}><button disabled={busy} onClick={() => void query()}>查询原同步</button><button disabled={busy || !canReplay(pending, user)} onClick={() => void run(pending)}>重试原同步</button>{rejected && <button disabled={busy} onClick={() => { remember(null); setRejected(false); }}>原请求被拒绝，结束本次恢复</button>}</div>{!canReplay(pending, user) && <p>原同步属于之前的会话，只能查询记录。</p>}</div>}</section>;
}
