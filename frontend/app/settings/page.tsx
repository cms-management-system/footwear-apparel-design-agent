"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import s from "./settings.module.css";

type User = { username: string; display_name: string; role: "manager" | "designer" };
type Member = { username: string; display_name: string; active: boolean };

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api/design-auth${path}`, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined, cache: "no-store",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "操作未完成，请重试");
  return data;
}

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [resetFor, setResetFor] = useState("");
  const [resetPassword, setResetPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    const me = await request<User>("/me");
    setUser(me);
    if (me.role === "manager") setMembers((await request<{ items: Member[] }>("/staff")).items);
  }, []);
  useEffect(() => { refresh().catch(() => window.location.replace("/login")); }, [refresh]);

  async function run(action: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (cause) { setError(cause instanceof Error ? cause.message : "操作失败"); }
    finally { setBusy(false); }
  }
  function changeMyPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(async () => {
      await request("/password", "POST", { current_password: currentPassword, new_password: newPassword });
      setCurrentPassword(""); setNewPassword("");
      window.location.assign("/login");
    });
  }
  function updateMember(member: Member, action: "activate" | "deactivate" | "reset_password") {
    void run(async () => {
      await request(`/staff/${encodeURIComponent(member.username)}`, "PATCH", { action, password: action === "reset_password" ? resetPassword : undefined });
      await refresh(); setResetFor(""); setResetPassword("");
      setNotice(action === "reset_password" ? `已重置 ${member.display_name} 的密码，旧登录已失效。` : `${member.display_name} 已${action === "activate" ? "恢复" : "停用"}。`);
    });
  }
  function logout() {
    void run(async () => {
      await request("/logout", "POST");
      window.location.assign("/login");
    });
  }
  if (!user) return <main className={s.page}><p className={s.muted}>正在打开设置…</p></main>;
  return <main className={s.page}>
    <div className={s.crumb}><Link href="/handoffs">需求交接</Link><span>/</span><strong>设置</strong></div>
    <header className={s.header}><div><p>工作区设置</p><h1>账号与成员</h1><span>设计人员自行注册，设计总监管理成员状态。</span></div><Link className={s.back} href="/handoffs">返回需求交接</Link></header>
    {error && <p role="alert" className={s.error}>{error}</p>}
    {notice && <p role="status" className={s.notice}>{notice}</p>}
    <div className={s.layout}>
      <nav className={s.nav} aria-label="设置导航"><a href="#my-account">我的账号</a>{user.role === "manager" && <a href="#members">团队成员</a>}<button type="button" disabled={busy} onClick={logout}>退出登录</button></nav>
      <div className={s.content}>
        <section id="my-account" className={s.panel} aria-labelledby="my-account-heading"><div className={s.panelHead}><div><h2 id="my-account-heading">我的账号</h2><p>你的工作区身份与登录信息。</p></div><span className={s.role}>{user.role === "manager" ? "设计总监" : "设计人员"}</span></div>
          <div className={s.identity}><div><small>姓名</small><strong>{user.display_name}</strong></div><div><small>登录账号</small><strong>{user.username}</strong></div></div>
          <form onSubmit={changeMyPassword} className={s.passwordForm}><h3>修改密码</h3><div className={s.formGrid}><label>当前密码<input type="password" autoComplete="current-password" value={currentPassword} onChange={event => setCurrentPassword(event.target.value)} required /></label><label>新密码<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={newPassword} onChange={event => setNewPassword(event.target.value)} required placeholder="至少 12 位" /></label></div><button disabled={busy}>保存新密码</button><p>保存后会退出当前账号，请使用新密码重新登录。</p></form>
        </section>
        {user.role === "manager" && <>
          <section id="members" className={s.panel} aria-labelledby="members-heading"><div className={s.panelHead}><div><h2 id="members-heading">团队成员</h2><p>设计人员从登录页自行注册；总监在这里管理已注册账号。</p></div><span className={s.count}>{members.length} 人</span></div>
            {members.length ? <ul className={s.memberList}>{members.map(member => <li key={member.username}><div className={s.avatar} aria-hidden="true">{member.display_name.slice(0, 1)}</div><div className={s.memberInfo}><strong>{member.display_name}</strong><span>{member.username}</span></div><span className={member.active ? s.active : s.inactive}>{member.active ? "使用中" : "已停用"}</span><div className={s.memberActions}><button type="button" disabled={busy} onClick={() => { setResetFor(resetFor === member.username ? "" : member.username); setResetPassword(""); }}>重置密码</button><button type="button" disabled={busy} onClick={() => updateMember(member, member.active ? "deactivate" : "activate")}>{member.active ? "停用" : "恢复"}</button></div>{resetFor === member.username && <div className={s.resetRow}><label>为 {member.display_name} 设置新密码<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={resetPassword} onChange={event => setResetPassword(event.target.value)} placeholder="至少 12 位" /></label><button type="button" disabled={busy || resetPassword.length < 12} onClick={() => updateMember(member, "reset_password")}>确认重置</button></div>}</li>)}</ul> : <p className={s.empty}>还没有设计人员。成员可以从登录页自行注册。</p>}
          </section>
        </>}
      </div>
    </div>
  </main>;
}
