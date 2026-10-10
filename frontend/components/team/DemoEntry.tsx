"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { bindDesignContext, errorText, isAbort, type TeamUser } from "@/lib/team-api";
import { navigateDemo, resolveDemoRole } from "@/lib/demo-access";
import { announceSessionChange } from "./TeamSession";
import s from "@/app/handoffs/handoff.module.css";
export default function DemoEntry({ entryRole: role }: { entryRole: TeamUser["role"] }) {
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); bindDesignContext(null); setError("");
    resolveDemoRole(role, controller.signal).then(() => {
      if (controller.signal.aborted) return;
      announceSessionChange(); navigateDemo(role);
    }).catch(cause => { if (!controller.signal.aborted && !isAbort(cause)) setError(errorText(cause)); });
    return () => controller.abort();
  }, [role, retry]);
  return <main className={s.shell}><section className={s.detail} aria-busy={!error}>
    <h1>{role === "designer" ? "设计员工演示" : "设计经理演示"}</h1>
    {error ? <><p role="alert" className={s.error}>{error}</p><div className={s.headerActions}><button onClick={() => setRetry(n => n + 1)}>重新连接</button><Link href="/login">团队登录入口</Link></div></> : <p role="status">正在进入演示工作区…</p>}
  </section></main>;
}
