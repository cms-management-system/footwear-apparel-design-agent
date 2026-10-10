"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { TeamBoundary } from "@/components/team/TeamSession";
import { agentApi, type ProjectEntry } from "@/lib/agent-api";
import { errorText, type TeamUser } from "@/lib/team-api";
import { ProjectCard } from "@/components/workspace/CreationHome";
import { DirectRecovery, useDirectCreation } from "@/components/workspace/useDirectCreation";
import { ProjectOperationRecovery } from "@/components/workspace/ProjectMenu";
import w from "@/components/workspace/workspace.module.css";
import s from "@/app/handoffs/handoff.module.css";
export default function ProjectsPage() { return <TeamBoundary>{user => <Projects user={user} />}</TeamBoundary>; }
function Projects({ user }: { user: TeamUser }) {
  const [items, setItems] = useState<ProjectEntry[]>([]); const [total, setTotal] = useState(0); const [offset, setOffset] = useState(0); const [filter, setFilter] = useState(""); const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [retry, setRetry] = useState(0);
  const creation = useDirectCreation(user);
  useEffect(() => {
    let current = true; setLoading(true); setError("");
    agentApi.projects(offset, 20).then(list => { if (current) { setItems(list.items); setTotal(list.total ?? list.items.length); } }).catch(cause => { if (current) { setItems([]); setError(errorText(cause)); } }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [offset, retry]);
  const shown = items.filter(item => !filter || item.source_mode === filter);
  return <main className={s.shell}><header className={s.topline}><div><p className={s.kicker}>DESIGN STUDIO / 设计项目</p><h1>{user.role === "manager" ? "团队的设计，在这里继续。" : "把你的想法，变成设计。"}</h1><p className={s.subtle}>自主创意与上游定向任务分别记录，完整文字、图片和版本都保存在项目里。</p></div><div className={s.headerActions}>{user.role === "designer" && <button className={s.primaryLink} disabled={creation.busy || !!creation.pending} onClick={() => void creation.blank()}>＋ 新建项目</button>}<Link href="/handoffs">{user.role === "manager" ? "上游需求收件" : "我的上游任务"} →</Link></div></header>
    <DirectRecovery creation={creation} user={user} /><ProjectOperationRecovery user={user} onChanged={() => setRetry(v => v + 1)} /><section className={s.detail}><div className={s.toolbar}><label>当前页来源<select value={filter} onChange={e => setFilter(e.target.value)}><option value="">全部设计</option><option value="independent">自主设计</option><option value="upstream">上游需求</option></select></label><button disabled={loading} onClick={() => setRetry(n => n + 1)}>刷新项目</button></div>
      {loading ? <p role="status" className={s.empty}>正在读取授权项目…</p> : error ? <p role="alert" className={s.error}>{error}</p> : !shown.length ? <div className={s.empty}><h2>{filter ? "当前页暂无此类设计" : "从一份想法开始"}</h2><p>{user.role === "designer" ? "你可以创建设计，或打开负责人分派的上游任务。" : "团队成员创建自主设计或接收分派上游需求后，可在这里查看。"}</p>{user.role === "designer" && <button className={s.primaryLink} disabled={creation.busy || !!creation.pending} onClick={() => void creation.blank()}>新建项目 →</button>}</div> : <div className={w.projectGrid}>{shown.map(item => <ProjectCard key={item.id} project={item} user={user} onChanged={() => setRetry(v => v + 1)} />)}</div>}
      <nav className={s.toolbar} aria-label="设计项目分页"><button disabled={loading || !offset} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><span>{Math.floor(offset / 20) + 1} · 共 {total} 个项目</span><button disabled={loading || offset + items.length >= total} onClick={() => setOffset(n => n + 20)}>下一页</button></nav>
    </section></main>;
}
