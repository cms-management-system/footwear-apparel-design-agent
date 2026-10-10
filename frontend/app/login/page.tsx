"use client";

import AuthEntryGate from "@/components/team/AuthEntryGate";
import { useState, type FormEvent } from "react";
import { appPath } from "@/lib/app-path";
import { teamApi, errorText } from "@/lib/team-api";
import { announceSessionChange } from "@/components/team/TeamSession";
import s from "../handoffs/handoff.module.css";

export default function LoginPage() { return <AuthEntryGate><LoginForm /></AuthEntryGate>; }
function LoginForm() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError("");
    try {
      await teamApi.login(username.trim(), password);
      const user = await teamApi.me();
      announceSessionChange();
      window.location.assign(appPath(user.role === "designer" ? "/" : "/handoffs"));
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  }
  return <main className={s.loginWrap}><section className={s.loginCard}>
    <p className={s.kicker}>DESIGN STUDIO / 鞋服设计</p><h1>把需求，做成方案。</h1>
    <p className={s.subtle}>登录团队账号，继续你的设计任务。</p>
    <form onSubmit={login} className={s.loginForm} aria-busy={busy}>
      <label>账号<input name="username" value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} maxLength={80} disabled={busy} required /></label>
      <label>密码<input name="password" type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" disabled={busy} required /></label>
      {error && <p className={s.error} role="alert">{error}</p>}
      <button className={s.primary} disabled={busy}>{busy ? "正在登录…" : "进入工作区 →"}</button>
    </form>
    <p className={s.loginRegister}>设计人员处理自己的任务；负责人接收、分派与审查。账号由团队管理者开通。</p>
  </section></main>;
}
