"use client";
import { useEffect, useRef, useState } from "react";
import { appPath } from "@/lib/app-path";
import { canReplay, type FrozenAction } from "@/lib/design-workflow";
import { executeDirect, freezeDirect, loadDirect, queryDirect, type DirectInput, type DirectResult } from "@/lib/direct-create";
import { currentDesignUser, errorText, type TeamUser } from "@/lib/team-api";
import s from "./workspace.module.css";

export function useDirectCreation(user: TeamUser, onCreated?: (input: DirectInput) => void) {
  const [pending, setPending] = useState<FrozenAction | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const alive = useRef(true); const lock = useRef(false);
  useEffect(() => { alive.current = true; setPending(loadDirect(user)); return () => { alive.current = false; }; }, [user]);
  const valid = () => alive.current && currentDesignUser() === user;
  const done = (r: DirectResult, a: FrozenAction) => { if (!valid()) return; setPending(null); onCreated?.(JSON.parse(a.body) as DirectInput); window.location.assign(appPath(`/projects/${r.project_id}`)); };
  async function start(input: DirectInput) {
    if (user.role !== "designer" || lock.current || loadDirect(user)) return;
    lock.current = true; setBusy(true); setError("");
    try { const a = freezeDirect(user, input); setPending(a); done(await executeDirect(a, user), a); }
    catch (cause) { if (valid()) { setError(errorText(cause)); setPending(loadDirect(user)); } }
    finally { lock.current = false; if (valid()) setBusy(false); }
  }
  async function recover(retry = false) {
    const a = loadDirect(user); if (!a || lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try { done(await (retry ? executeDirect(a, user) : queryDirect(a, user)), a); }
    catch (cause) { if (valid()) { setError(errorText(cause)); setPending(loadDirect(user)); } }
    finally { lock.current = false; if (valid()) setBusy(false); }
  }
  const blank = () => start({ entry_mode: "blank", text: "", output_kind: "effect_image", intent: "none", authorized: false });
  return { start, blank, busy, pending, error, recover };
}
export function DirectRecovery({ creation, user }: { creation: ReturnType<typeof useDirectCreation>; user: TeamUser }) {
  return <>{creation.error && <p role="alert" className={s.error}>{creation.error}</p>}{creation.pending && <section className={s.notice} aria-label="原创建操作恢复"><p>原创建操作待核对。刷新不会重新创建或生成。</p><button disabled={creation.busy} onClick={() => void creation.recover()}>查询原创建操作</button><button disabled={creation.busy || !canReplay(creation.pending, user)} onClick={() => void creation.recover(true)}>重试原创建操作</button></section>}</>;
}
