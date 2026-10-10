"use client";

import { useCallback, useEffect, useState } from "react";
import Image from "next/image";
import s from "./handoff.module.css";

type User = { username: string; display_name: string; role: "manager" | "designer" };
type Package = { requirement_desc: string; priority?: string | null; priority_basis?: string | null; signal_ids: string[]; constraints?: string[] | null; source: string };
type Handoff = { id: string; package_id: string; version: string; package: Package; status: string; assignee: string | null; project_id: number | null; submitted_version_id: string | null; manager_note: string };
type Staff = { username: string; display_name: string; active: boolean };
const statusText: Record<string, string> = { new: "待接收", accepted: "待分派", returned: "待产品澄清", assigned: "设计中", review: "待审核", sent: "设计已审核" };

async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error?.message || "操作未完成");
  return data;
}

export default function HandoffsPage() {
  const [user, setUser] = useState<User | null>(null);
  const [items, setItems] = useState<Handoff[]>([]);
  const [staff, setStaff] = useState<Staff[]>([]);
  const [selected, setSelected] = useState("");
  const [assignee, setAssignee] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const current = items.find(item => item.id === selected) || items[0];

  const refresh = useCallback(async () => {
    const [me, list] = await Promise.all([api<User>("/design-auth/me"), api<{ items: Handoff[] }>("/design-handoffs")]);
    setUser(me); setItems(list.items);
    if (me.role === "manager") setStaff((await api<{ items: Staff[] }>("/design-auth/staff")).items);
  }, []);
  useEffect(() => { refresh().catch(() => window.location.replace("/login")); }, [refresh]);

  async function perform(path: string, body?: unknown, success = "处理结果已保存") {
    setBusy(true); setError(""); setNotice("");
    try { await api(path, "POST", body); await refresh(); setNotice(success); setNote(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "操作失败"); }
    finally { setBusy(false); }
  }
  async function sync() {
    await perform("/design-handoffs/sync", undefined, "已检查知需；只有真实且已批准的需求会进入收件箱");
  }
  if (!user) return <main className={s.shell}><p>正在进入工作区…</p></main>;
  const decision = (action: string, extra?: object) => void perform(`/design-handoffs/${encodeURIComponent(current.id)}/decision`, { action, note, ...extra });
  return <main className={s.shell}>
    <div className={s.topline}><div><p className={s.kicker}>款式工场 / {user.role === "manager" ? "设计负责人" : "设计人员"}</p><h1>{user.role === "manager" ? "需求交接与设计审核" : "我的设计任务"}</h1><p className={s.subtle}>{user.role === "manager" ? "接收已批准需求，确认可行性后分派；审核结果同步给产品团队。" : "只显示分派给你的需求。先核对原需求，再与智能体推敲设计。"}</p></div><div className={s.headerActions}><span>{user.display_name}</span></div></div>
    {user.role === "manager" && <div className={s.toolbar}><button className={s.primary} disabled={busy} onClick={() => void sync()}>同步知需已批准需求</button><span>重复同步不会建立重复任务。</span></div>}
    {error && <p role="alert" className={s.error}>{error}</p>}{notice && <p role="status" className={s.notice}>{notice}</p>}
    <div className={s.columns}><section className={s.list}><h2>{user.role === "manager" ? "需求收件箱" : "分派给我"} <small>{items.length}</small></h2>{items.length ? items.map(item => <button key={item.id} className={`${s.listItem} ${current?.id === item.id ? s.active : ""}`} onClick={() => { setSelected(item.id); setNote(""); setAssignee(item.assignee || ""); }}><span className={s.listTop}>{item.package_id} <em>{statusText[item.status] || item.status}</em></span><strong>{item.package.requirement_desc}</strong><small>{item.version} · {item.package.signal_ids.length} 条来源信号</small></button>) : <p className={s.empty}>{user.role === "manager" ? "暂无新需求。点击上方按钮从知需同步。" : "目前没有分派给你的设计任务。"}</p>}</section>
      <section className={s.detail}>{current ? <><div className={s.detailHead}><div><p className={s.kicker}>{current.package_id} / {current.version}</p><h2>{current.package.requirement_desc}</h2></div><span className={s.badge}>{statusText[current.status] || current.status}</span></div><div className={s.facts}><div><small>来源</small><b>{current.package.source}</b></div><div><small>优先级</small><b>{current.package.priority || "待确认"}</b></div><div><small>信号数</small><b>{current.package.signal_ids.length}</b></div></div>{current.package.priority_basis && <p className={s.subtle}>优先级依据：{current.package.priority_basis}</p>}
        <div className={s.section}><h3>设计约束</h3>{current.package.constraints?.length ? <ul>{current.package.constraints.map((value, index) => <li key={index}>{value}</li>)}</ul> : <p>需求包没有结构化约束；设计前请与产品团队核实。</p>}</div>
        {current.manager_note && <div className={s.section}><h3>负责人意见</h3><p>{current.manager_note}</p></div>}
        {current.submitted_version_id && user.role === "manager" && <div className={s.section}><h3>设计人员提交的方案</h3><Image className={s.reviewImage} src={`/api/design-versions/${current.submitted_version_id}/image`} alt="待审核的鞋服设计方案" width={650} height={650} unoptimized /><p>设计版本：{current.submitted_version_id}</p></div>}
        {user.role === "manager" ? <div className={s.actions}><h3>负责人处理</h3><textarea value={note} onChange={e => setNote(e.target.value)} placeholder="可行性判断、澄清问题或审核意见" rows={3} />{["new", "returned"].includes(current.status) && <><button className={s.primary} disabled={busy} onClick={() => decision("accept")}>确认接收</button>{current.status === "new" && <button disabled={busy || !note.trim()} onClick={() => decision("return")}>退回澄清</button>}</>}{current.status === "accepted" && <><label>分派设计人员<select value={assignee} onChange={e => setAssignee(e.target.value)}><option value="">请选择</option>{staff.filter(person => person.active).map(person => <option key={person.username} value={person.username}>{person.display_name} · {person.username}</option>)}</select></label><button className={s.primary} disabled={busy || !assignee} onClick={() => decision("assign", { assignee })}>确认分派</button><button disabled={busy || !note.trim()} onClick={() => decision("return")}>退回产品团队</button></>}{current.status === "review" && <><button className={s.primary} disabled={busy} onClick={() => void perform(`/design-handoffs/${encodeURIComponent(current.id)}/review`, { action: "approve", note })}>审核通过并同步产品团队</button><button disabled={busy} onClick={() => void perform(`/design-handoffs/${encodeURIComponent(current.id)}/review`, { action: "revise", note })}>退回设计人员修改</button></>}</div> : <div className={s.actions}><h3>我的下一步</h3>{current.project_id && <a className={s.primaryLink} href={`/?project=${current.project_id}`}>打开设计项目，与智能体协作</a>}{current.status === "assigned" && <button disabled={busy} onClick={() => void perform(`/design-handoffs/${encodeURIComponent(current.id)}/submit`)}>已确认方案，提交负责人审核</button>}{current.status === "review" && <p>已提交，等待负责人审核。</p>}</div>}</> : <div className={s.empty}>选择一条需求查看详情。</div>}</section></div>
  </main>;
}
