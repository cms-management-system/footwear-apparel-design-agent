"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { TeamBoundary, announceSessionChange } from "@/components/team/TeamSession";
import { appPath } from "@/lib/app-path";
import { teamApi, errorText, isAbort, type TeamMember, type TeamUser } from "@/lib/team-api";
import { executeAction, freezeAction, type FrozenAction } from "@/lib/design-workflow";
import s from "./settings.module.css";

export default function SettingsPage() { return <TeamBoundary>{user => <Settings user={user} />}</TeamBoundary>; }
function Settings({ user }: { user: TeamUser }) {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(user.role === "manager");
  const [reload, setReload] = useState(0);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [resetFor, setResetFor] = useState("");
  const [resetPassword, setResetPassword] = useState("");
  const [newName, setNewName] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [memberPassword, setMemberPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const lock = useRef(false);
  const mounted = useRef(true);
  // Password-bearing actions stay in memory only, and disappear on identity/session change.
  const pending = useRef<FrozenAction | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; pending.current = null; }; }, []);
  useEffect(() => {
    if (user.role !== "manager") return;
    const controller = new AbortController(); let current = true; setLoading(true);
    teamApi.staff(controller.signal).then(result => { if (current) setMembers(result.items); }).catch(cause => { if (current && !isAbort(cause)) setError(errorText(cause)); }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; controller.abort(); };
  }, [user.role, reload]);
  async function mutate(path: string, body: unknown, method: string, success: () => void) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const bytes = JSON.stringify(body);
      if (!pending.current || pending.current.path !== path || pending.current.body !== bytes || pending.current.method !== method) pending.current = freezeAction(user, path, body, method);
      await executeAction(pending.current, user);
      pending.current = null;
      if (mounted.current) success();
    } catch (cause) { if (mounted.current) setError(errorText(cause)); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  function changePassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void mutate("/design-auth/password", { current_password: currentPassword, new_password: newPassword }, "POST", () => { setCurrentPassword(""); setNewPassword(""); announceSessionChange(); window.location.assign(appPath("/login")); });
  }
  function createMember(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void mutate("/design-auth/staff", { username: newUsername, display_name: newName.trim(), password: memberPassword }, "POST", () => { setNewName(""); setNewUsername(""); setMemberPassword(""); setReload(n => n + 1); setNotice("设计人员账号已创建。分派任务后，该成员可以查看相应任务。"); });
  }
  function updateMember(member: TeamMember, action: "activate" | "deactivate" | "reset_password") {
    if (["deactivate", "reset_password"].includes(action) && !window.confirm(`${action === "deactivate" ? "停用" : "重置密码："}${member.display_name}？这会撤销该成员已有登录会话。`)) return;
    void mutate(`/design-auth/staff/${encodeURIComponent(member.username)}`, { action, ...(action === "reset_password" ? { password: resetPassword } : {}) }, "PATCH", () => { setResetFor(""); setResetPassword(""); setReload(n => n + 1); setNotice(`${member.display_name} 的账号状态已更新。`); });
  }
  async function logout() {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError("");
    try { await teamApi.logout(); announceSessionChange(); window.location.assign(appPath("/login")); }
    catch (cause) { setError(errorText(cause)); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  if (user.access_mode === "demo") return <main className={s.page}><header className={s.header}><div><p>DESIGN STUDIO / 演示设置</p><h1>演示身份与团队</h1><span>当前为合成演示身份，账号操作暂时冻结。</span></div></header>
    <section className={s.panel}><h2>{user.display_name}</h2><p>你可以直接使用当前角色的工作区。任务分派与版本审批仍按实际权限执行。</p><p>演示期间不修改密码、开通或停用账号。</p><div className={s.identity}><a href={appPath(`/demo/${user.role}`)}>返回演示入口</a><a href={appPath(`/demo/${user.role === "designer" ? "manager" : "designer"}`)}>切换为{user.role === "designer" ? "设计经理" : "设计员工"}</a></div></section>
  </main>;
  const unavailable = busy || !user.auth_context_id;
  return <main className={s.page}><header className={s.header}><div><p>DESIGN STUDIO / 设置</p><h1>账号与团队</h1><span>管理登录信息，让每项设计都有明确负责人。</span></div><button className={s.back} onClick={() => void logout()} disabled={unavailable}>退出登录</button></header>
    {error && <p role="alert" className={s.error}>{error}</p>}{notice && <p role="status" className={s.notice}>{notice}</p>}
    <div className={s.layout}><nav className={s.nav} aria-label="设置导航"><a href="#my-account">我的账号</a>{user.role === "manager" && <><a href="#members">团队成员</a><a href="#create-member">开通账号</a></>}</nav><div className={s.content}>
      <section id="my-account" className={s.panel} aria-labelledby="my-account-heading"><div className={s.panelHead}><div><h2 id="my-account-heading">我的账号</h2><p>角色由团队授权决定。</p></div><span className={s.role}>{user.role === "manager" ? "设计负责人" : "设计人员"}</span></div><div className={s.identity}><div><small>姓名</small><strong>{user.display_name}</strong></div><div><small>账号</small><strong>{user.username}</strong></div></div>
        <form onSubmit={changePassword} className={s.passwordForm}><h3>修改密码</h3><div className={s.formGrid}><label>当前密码<input type="password" name="current-password" autoComplete="current-password" value={currentPassword} onChange={e => setCurrentPassword(e.target.value)} disabled={unavailable} required /></label><label>新密码<input type="password" name="new-password" autoComplete="new-password" minLength={12} maxLength={128} value={newPassword} onChange={e => setNewPassword(e.target.value)} disabled={unavailable} required placeholder="12–128 位" /></label></div><button disabled={unavailable}>保存新密码</button><p>保存后已有登录会话失效，请使用新密码重新登录。</p></form></section>
      {user.role === "manager" && <><section id="members" className={s.panel} aria-labelledby="members-heading"><div className={s.panelHead}><div><h2 id="members-heading">团队成员</h2><p>停用与密码重置均会撤销对应会话。</p></div><button disabled={busy || loading} onClick={() => setReload(n => n + 1)}>刷新成员</button></div>
        {loading ? <p className={s.empty} role="status">正在读取成员…</p> : members.length ? <ul className={s.memberList}>{members.map(member => <li key={member.username}><div className={s.avatar} aria-hidden="true">{member.display_name.slice(0, 1)}</div><div className={s.memberInfo}><strong>{member.display_name}</strong><span>{member.username}</span></div><span className={member.active ? s.active : s.inactive}>{member.active ? "使用中" : "已停用"}</span><div className={s.memberActions}><button type="button" disabled={unavailable} onClick={() => { setResetFor(resetFor === member.username ? "" : member.username); setResetPassword(""); }}>重置密码</button><button type="button" disabled={unavailable} onClick={() => updateMember(member, member.active ? "deactivate" : "activate")}>{member.active ? "停用" : "恢复"}</button></div>{resetFor === member.username && <form className={s.resetRow} onSubmit={event => { event.preventDefault(); updateMember(member, "reset_password"); }}><label>为 {member.display_name} 设置新密码<input type="password" autoComplete="new-password" minLength={12} maxLength={128} value={resetPassword} onChange={e => setResetPassword(e.target.value)} disabled={unavailable} required placeholder="12–128 位" /></label><button disabled={unavailable || resetPassword.length < 12}>确认重置</button><button type="button" disabled={busy} onClick={() => { setResetFor(""); setResetPassword(""); }}>取消</button></form>}</li>)}</ul> : <p className={s.empty}>暂无设计人员。可以在下方开通第一个账号。</p>}</section>
      <section id="create-member" className={s.panel}><div className={s.panelHead}><div><h2>开通设计人员账号</h2><p>新成员只能处理明确分派给自己的任务。</p></div></div><form className={s.passwordForm} onSubmit={createMember}><div className={s.formGrid}><label>姓名<input value={newName} onChange={e => setNewName(e.target.value)} maxLength={100} autoComplete="off" disabled={unavailable} required /></label><label>登录账号<input value={newUsername} onChange={e => setNewUsername(e.target.value)} pattern="[a-z][a-z0-9_-]{2,39}" title="以小写字母开头，3–40位小写字母、数字、下划线或横线" autoComplete="off" spellCheck={false} disabled={unavailable} required /></label><label>初始密码<input type="password" value={memberPassword} onChange={e => setMemberPassword(e.target.value)} autoComplete="new-password" minLength={12} maxLength={128} disabled={unavailable} required /></label></div><button disabled={unavailable || !newName.trim()}>开通账号</button></form></section></>}
    </div></div>
  </main>;
}
