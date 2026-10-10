"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import s from "../handoffs/handoff.module.css";

type ValidationIssue = { loc?: (string | number)[]; type?: string };

function registrationError(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return "注册失败，请检查填写内容后重试。";
  const fieldNames: Record<string, string> = {
    display_name: "姓名", username: "登录账号", password: "密码", confirm_password: "确认密码",
  };
  const issue = detail[0] as ValidationIssue | undefined;
  const field = fieldNames[String(issue?.loc?.at(-1))] || "填写内容";
  if (field === "登录账号") return "登录账号须以英文字母小写开头，使用 3–40 位小写字母、数字、下划线或横线。";
  if (field === "密码" || field === "确认密码") return `${field}须为 12–128 位，请重新检查。`;
  if (field === "姓名") return "请填写姓名，最多 100 个字。";
  return "注册信息不符合要求，请检查姓名、账号和密码。";
}

export default function RegisterPage() {
  const [displayName, setDisplayName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!/^[a-z][a-z0-9_-]{2,39}$/.test(username)) {
      setError("登录账号须以小写英文字母开头，共 3–40 位；其余可用数字、下划线或横线。");
      return;
    }
    if (password !== confirmPassword) { setError("两次输入的密码不一致"); return; }
    setBusy(true); setError("");
    try {
      const response = await fetch("/api/design-auth/register", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName, username, password, confirm_password: confirmPassword }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(registrationError(data.detail));
      window.location.assign("/handoffs");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "注册失败"); }
    finally { setBusy(false); }
  }

  return <main className={s.loginWrap}><section className={s.loginCard}>
    <p className={s.kicker}>款式工场 / 设计人员注册</p><h1>创建你的账号</h1>
    <p className={s.subtle}>注册后进入设计工作区。设计任务由设计总监分派；总监权限不能通过注册获得。</p>
    <form onSubmit={register} className={s.loginForm}>
      <label>姓名<input value={displayName} onChange={event => setDisplayName(event.target.value)} maxLength={100} autoComplete="name" required /></label>
      <label>登录账号<input value={username} onChange={event => setUsername(event.target.value)} title="英文小写字母开头，共 3–40 位，可用数字、下划线和横线" autoComplete="username" required /></label>
      <label>设置密码<input type="password" value={password} onChange={event => setPassword(event.target.value)} minLength={12} maxLength={128} autoComplete="new-password" required placeholder="至少 12 位" /></label>
      <label>确认密码<input type="password" value={confirmPassword} onChange={event => setConfirmPassword(event.target.value)} minLength={12} maxLength={128} autoComplete="new-password" required /></label>
      {error && <p className={s.error} role="alert">{error}</p>}
      <button className={s.primary} disabled={busy}>{busy ? "正在创建…" : "注册并进入工作台"}</button>
    </form>
    <p className={s.loginRegister}>已有账号？<Link href="/login">返回登录</Link></p>
  </section></main>;
}
