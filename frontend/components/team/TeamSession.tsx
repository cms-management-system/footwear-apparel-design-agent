"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { ApiError, bindDesignContext, DESIGN_SESSION_INVALIDATED_EVENT, errorText, isAbort, teamApi, userIdentity, type TeamUser } from "@/lib/team-api";
import { appPath } from "@/lib/app-path";
import { demoRole, navigateTeamWorkspace, resolveDemoRole } from "@/lib/demo-access";
import demoStyle from "./demo-session.module.css";
import s from "@/app/handoffs/handoff.module.css";

export function announceSessionChange() {
  // No account details or credentials in shared browser storage.
  try { localStorage.setItem("design-session-change", String(Date.now())); } catch { /* Storage may be blocked; current tab still invalidates. */ }
  window.dispatchEvent(new Event("design-session-change"));
}

export function TeamBoundary({ children }: { children: (user: TeamUser) => ReactNode }) {
  const [user, setUser] = useState<TeamUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const entryConsumed = useRef(false);
  const verify = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const run = ++generation.current;
    setChecking(true); setError(null); bindDesignContext(null);
    try {
      let next = await teamApi.me(controller.signal);
      if (run !== generation.current || controller.signal.aborted) return;
      if (next.access_mode === "demo" && !entryConsumed.current) {
        const url = new URL(window.location.href);
        const selected = demoRole(url.searchParams.get("demo_role"));
        const switched = !!selected && next.role !== selected;
        if (switched) next = await resolveDemoRole(selected!, controller.signal);
        if (run !== generation.current || controller.signal.aborted) return;
        entryConsumed.current = true;
        if (selected) {
          url.searchParams.delete("demo_role"); window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
        }
        if (switched) { announceSessionChange(); return; }
      }
      if (next.access_mode === "demo" && next.role === "manager" && window.location.pathname === appPath("/")) {
        navigateTeamWorkspace("manager"); return;
      }
      if (run === generation.current && !controller.signal.aborted) { bindDesignContext(next.auth_context_id ?? null, next); setUser(next); }
    } catch (cause) {
      if (run !== generation.current || controller.signal.aborted || isAbort(cause)) return;
      bindDesignContext(null); setUser(null); setError(cause instanceof Error ? cause : new Error(errorText(cause)));
    } finally { if (run === generation.current) setChecking(false); }
  }, []);
  useEffect(() => {
    void verify();
    const onVisible = () => { if (document.visibilityState === "visible") void verify(); };
    const onStorage = (event: StorageEvent) => { if (event.key === "design-session-change") { setUser(null); void verify(); } };
    const changed = () => { setUser(null); void verify(); };
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("storage", onStorage);
    window.addEventListener("design-session-change", changed);
    window.addEventListener(DESIGN_SESSION_INVALIDATED_EVENT, changed);
    return () => { generation.current = -1; request.current?.abort(); window.removeEventListener("focus", onVisible); document.removeEventListener("visibilitychange", onVisible); window.removeEventListener("storage", onStorage); window.removeEventListener("design-session-change", changed); window.removeEventListener(DESIGN_SESSION_INVALIDATED_EVENT, changed); };
  }, [verify]);
  const loginNeeded = error instanceof ApiError && [401, 403].includes(error.status);
  return <>
    {checking && <main className={s.shell} aria-busy="true"><p role="status" className={s.subtle}>正在确认工作区身份…</p></main>}
    {error && <main className={s.shell}><section className={s.detail}><h1>{loginNeeded ? "请重新登录" : "暂时无法打开工作区"}</h1><p role="alert" className={s.error}>{error.message}</p><div className={s.headerActions}><button type="button" onClick={() => void verify()}>重新连接</button><Link className={s.primaryLink} href="/login">前往登录</Link></div></section></main>}
    {user && <div className={user.access_mode === "demo" ? demoStyle.frame : undefined} key={userIdentity(user)} hidden={checking || !!error}>
      {user.access_mode === "demo" && <aside className={demoStyle.banner} aria-label="演示身份"><span>{user.display_name} · 合成演示身份</span><Link href={user.role === "designer" ? "/demo/manager" : "/demo/designer"}>切换为{user.role === "designer" ? "设计经理" : "设计员工"}</Link></aside>}
      {children(user)}</div>}
  </>;
}
