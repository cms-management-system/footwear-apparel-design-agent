"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import s from "../handoffs/handoff.module.css";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await fetch("/api/design-auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.error?.message || "登录失败");
      window.location.assign("/handoffs");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "登录失败"); }
    finally { setBusy(false); }
  }
  return <main className={s.loginWrap}><section className={s.loginCard}>
    <p className={s.kicker}>款式工场 / 团队入口</p><h1>进入设计工作区</h1>
    <p className={s.subtle}>设计负责人接收和分派产品需求；设计人员处理分派给自己的款式任务。</p>
    <form onSubmit={login} className={s.loginForm}><label>账号<input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required /></label>
      <label>密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required /></label>
      {error && <p className={s.error} role="alert">{error}</p>}<button className={s.primary} disabled={busy}>{busy ? "正在登录…" : "登录工作台"}</button></form>
    <p className={s.loginRegister}>还没有设计人员账号？<Link href="/register">自行注册</Link></p>
  </section></main>;
}
