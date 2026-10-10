"use client";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { ProjectEntry } from "@/lib/agent-api";
import { canReplay } from "@/lib/design-workflow";
import { executeProjectAction, freezeProjectAction, projectActions, PROJECT_ACTION_EVENT, queryProjectAction, type ProjectAction } from "@/lib/project-metadata";
import { currentDesignUser, errorText, type TeamUser } from "@/lib/team-api";
import s from "./workspace.module.css";

export default function ProjectMenu({ project, user, onChanged }: { project: ProjectEntry; user: TeamUser; onChanged: () => void }) {
  const [open, setOpen] = useState(false); const [mode, setMode] = useState<"rename" | "archive" | null>(null); const [title, setTitle] = useState(project.name); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const trigger = useRef<HTMLButtonElement>(null); const menu = useRef<HTMLDivElement>(null); const dialog = useRef<HTMLDialogElement>(null); const alive = useRef(true); const lock = useRef(false);
  const allowed = project.allowed_project_actions ?? [];
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, [user]);
  useEffect(() => {
    if (!open) return;
    const first = menu.current?.querySelector<HTMLElement>('[role="menuitem"]:not(:disabled)'); (first ?? menu.current)?.focus();
    const outside = (e: PointerEvent) => { if (!menu.current?.contains(e.target as Node) && !trigger.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside); return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  useEffect(() => { if (!mode) return; const d = dialog.current; if (d?.showModal) d.showModal(); else d?.setAttribute("open", ""); }, [mode]);
  const close = () => { setOpen(false); setMode(null); trigger.current?.focus(); };
  function keyboard(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key === "Tab") { setOpen(false); return; }
    const nodes = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])];
    if (!nodes.length || !["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault(); const at = nodes.indexOf(document.activeElement as HTMLButtonElement);
    nodes[e.key === "Home" ? 0 : e.key === "End" ? nodes.length - 1 : (at + (e.key === "ArrowDown" ? 1 : -1) + nodes.length) % nodes.length]?.focus();
  }
  async function submit() {
    if (!mode || lock.current || !allowed.includes(mode) || currentDesignUser() !== user) return;
    lock.current = true; setBusy(true); setError("");
    try { const a = freezeProjectAction(project.id, project.revision ?? 0, user, mode === "rename" ? title : undefined); await executeProjectAction(a, user); if (alive.current && currentDesignUser() === user) { close(); onChanged(); } }
    catch (cause) { if (alive.current && currentDesignUser() === user) setError(errorText(cause)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  return <div className={s.projectMenu}><button ref={trigger} aria-label={`${project.name}的项目菜单`} aria-haspopup="menu" aria-expanded={open} onClick={() => { setOpen(v => !v); setError(""); }} onKeyDown={e => { if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); } }}>⋯</button>
    {open && <div ref={menu} tabIndex={-1} role="menu" aria-label={`${project.name}操作`} onKeyDown={keyboard} className={s.menuPopup}><button role="menuitem" disabled={!allowed.includes("rename")} onClick={() => { setOpen(false); setTitle(project.name); setMode("rename"); }}>重命名</button><button role="menuitem" className={s.destructive} disabled={!allowed.includes("archive")} onClick={() => { setOpen(false); setMode("archive"); }}>删除项目</button>{!allowed.length && <p>此项目只读，请到任务管理查看。</p>}</div>}
    {mode && <dialog ref={dialog} aria-labelledby={`project-operation-${project.id}`} className={s.projectDialog} onCancel={e => { e.preventDefault(); if (!busy) close(); }} onClose={close}><h2 id={`project-operation-${project.id}`}>{mode === "rename" ? "重命名项目" : "删除项目？"}</h2><form onSubmit={e => { e.preventDefault(); void submit(); }}>{mode === "rename" ? <label>项目名称<input value={title} maxLength={120} onChange={e => setTitle(e.target.value)} /></label> : <p>将“{project.name}”从项目列表移除。项目历史和图片保留，不会删除已批准来源。</p>}{error && <p role="alert" className={s.error}>{error}</p>}<div><button type="button" disabled={busy} onClick={close}>取消</button><button type="submit" disabled={busy || (mode === "rename" && !title.trim())} className={mode === "archive" ? s.destructive : s.primary}>{busy ? "正在保存…" : mode === "rename" ? "保存名称" : "确认删除"}</button></div></form></dialog>}
  </div>;
}

/** List-level recovery remains visible even when an unknown archive already removed its card. */
export function ProjectOperationRecovery({ user, onChanged }: { user: TeamUser; onChanged: () => void }) {
  const [items, setItems] = useState<ProjectAction[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const lock = useRef(false); const alive = useRef(true);
  useEffect(() => { alive.current = true; const update = () => setItems(projectActions(user)); update(); window.addEventListener(PROJECT_ACTION_EVENT, update); return () => { alive.current = false; window.removeEventListener(PROJECT_ACTION_EVENT, update); }; }, [user]);
  async function recover(a: ProjectAction, retry: boolean) {
    if (lock.current) return; lock.current = true; setBusy(true); setError("");
    try { await (retry ? executeProjectAction(a, user) : queryProjectAction(a, user)); if (alive.current && currentDesignUser() === user) onChanged(); }
    catch (cause) { if (alive.current && currentDesignUser() === user) setError(errorText(cause)); }
    finally { lock.current = false; if (alive.current) setBusy(false); }
  }
  if (!items.length) return null;
  return <section className={s.notice} aria-label="项目管理操作恢复"><p>有项目操作待核对。刷新只读取原记录，不会再次删除或重命名。</p>{items.map(a => <div key={a.id}><span>项目 #{a.projectId} · {a.path.endsWith("/archive") ? "删除" : "重命名"}</span><button disabled={busy} onClick={() => void recover(a, false)}>查询原项目操作</button><button disabled={busy || !canReplay(a, user)} onClick={() => void recover(a, true)}>重试原项目操作</button></div>)}{error && <p role="alert" className={s.error}>{error}</p>}</section>;
}
