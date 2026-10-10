"use client";
import { useEffect, useState, type ReactNode } from "react";
import { ApiError, errorText, isAbort, teamApi } from "@/lib/team-api";
import { demoRole, navigateDemoEntry } from "@/lib/demo-access";
import s from "@/app/handoffs/handoff.module.css";
export default function AuthEntryGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"checking" | "ordinary" | "error">("checking");
  const [error, setError] = useState(""); const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setState("checking");
    teamApi.me(controller.signal).then(user => {
      if (controller.signal.aborted) return;
      if (user.access_mode === "demo") {
        const role = demoRole(new URLSearchParams(window.location.search).get("demo_role")) ?? user.role;
        navigateDemoEntry(role);
      } else setState("ordinary");
    }).catch(cause => {
      if (controller.signal.aborted || isAbort(cause)) return;
      if (cause instanceof ApiError && cause.status === 401) setState("ordinary");
      else { setError(errorText(cause)); setState("error"); }
    });
    return () => controller.abort();
  }, [retry]);
  if (state === "ordinary") return children;
  return <main className={s.shell} aria-busy={state === "checking"}>{state === "error" ? <><p role="alert" className={s.error}>{error}</p><button onClick={() => setRetry(n => n + 1)}>重新连接</button></> : <p role="status">正在确认工作区入口…</p>}</main>;
}
