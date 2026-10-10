"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { agentApi, type Capabilities, type Workspace } from "@/lib/agent-api";
import { ApiError, DESIGN_SESSION_INVALIDATED_EVENT, designContextKey, errorText, type TeamUser } from "@/lib/team-api";
import { rememberWorkspace } from "@/lib/engine-api";
import { appPath } from "@/lib/app-path";
import type { SelectedContext } from "@/lib/workspace-api";
import SourceRequirements from "@/components/team/SourceRequirements";
import DesignCanvas from "./DesignCanvas";
import ProjectConversation from "./ProjectConversation";
import { useCanvas } from "./useCanvas";
import s from "./workspace.module.css";

export default function ProjectWorkspace({ projectId, user }: { projectId: number; user: TeamUser }) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null); const [caps, setCaps] = useState<Capabilities | null>(null); const [error, setError] = useState(""); const [connection, setConnection] = useState(""); const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<SelectedContext | null>(null); const [tab, setTab] = useState<"canvas" | "chat">("canvas"); const [chatOpen, setChatOpen] = useState(true); const [sourceOpen, setSourceOpen] = useState(false); const [messageDirty, setMessageDirty] = useState(false);
  const editable = user.role === "designer" && !!workspace?.project_context && workspace.project_context.owner_subject === user.username && (!["review", "approved"].includes(workspace.project_context.status)) && (!workspace.source_binding?.handoff_id || workspace.source_binding.status === "assigned");
  // Upstream owner_subject is the current assigned designer under the formal backend contract.
  const canvasEditable = editable && !!workspace?.project_context?.allowed_actions.includes("save_canvas");
  const conversationEditable = editable && !!workspace?.project_context?.allowed_actions.includes("send_message");
  const placementKey = JSON.stringify((workspace?.creation_runs ?? []).filter(run => run.version_id).map(run => [run.id, run.version_id, run.canvas_placement]));
  const runProgressKey = JSON.stringify((workspace?.creation_runs ?? []).map(run => [run.id, run.status, run.stage]));
  useEffect(() => { let active = true; if (runProgressKey !== "[]") agentApi.capabilities(projectId).then(value => { if (active && !revoked.current) setCaps(value); }).catch(() => {}); return () => { active = false; }; }, [projectId, runProgressKey]);
  const canvas = useCanvas(projectId, user, canvasEditable, placementKey);
  const sourceTrigger = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (!sourceOpen) return; const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setSourceOpen(false); sourceTrigger.current?.focus(); } }; window.addEventListener("keydown", escape); return () => window.removeEventListener("keydown", escape); }, [sourceOpen]);
  const alive = useRef(true); const revoked = useRef(false); const headRevision = useRef(0);
  const accept = useCallback((value: Workspace) => {
    if (!alive.current || revoked.current || value.project?.id !== projectId || (value.revision ?? 0) < headRevision.current) return;
    rememberWorkspace(value); headRevision.current = value.revision ?? 0; setWorkspace(value); setError(""); setConnection("");
  }, [projectId]);
  const refresh = useCallback(async () => {
    try { const value = await agentApi.workspace(projectId); accept(value); }
    catch (cause) { if (cause instanceof ApiError && [401,403,404].includes(cause.status) && alive.current) { revoked.current = true; setWorkspace(null); setSelected(null); } throw cause; }
  }, [projectId, accept]);
  useEffect(() => {
    alive.current = true; revoked.current = false; headRevision.current = 0; let disposed = false; let source: EventSource | undefined; let checking = false; let timer: ReturnType<typeof setTimeout> | undefined;
    const context = designContextKey(); const valid = () => !disposed && context === designContextKey();
    const fail = (cause: unknown) => { if (!valid()) return; if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) { revoked.current = true; setWorkspace(null); source?.close(); if (timer) clearTimeout(timer); } setError(errorText(cause)); };
    refresh().catch(fail); agentApi.capabilities(projectId).then(value => { if (valid()) setCaps(value); }).catch(() => { if (valid()) setCaps(null); });
    function connect() {
      if (!valid() || revoked.current) return;
      if (typeof EventSource === "undefined") { timer = setTimeout(() => { refresh().catch(fail); connect(); }, 4000); return; }
      source = new EventSource(appPath(`/api/project/${projectId}/design-events`));
      source.addEventListener("workspace", event => { try { if (valid()) accept(JSON.parse((event as MessageEvent).data)); } catch { if (valid()) setConnection("正在核对项目更新…"); } });
      const reconnect = () => {
        if (checking || !valid() || revoked.current) return;
        checking = true; source?.close();
        // A closed stream may mean archive/revocation, not merely a network gap.
        void refresh().catch(fail).finally(() => { checking = false; if (valid() && !revoked.current) timer = setTimeout(connect, 2000); });
      };
      source.addEventListener("done", reconnect);
      source.addEventListener("archived", () => { revoked.current = true; setWorkspace(null); setSelected(null); source?.close(); if (timer) clearTimeout(timer); setError("此项目已从项目列表移除。"); });
      source.addEventListener("error", event => { try { const code = JSON.parse((event as MessageEvent).data).error?.code; if (["LOGIN_REQUIRED", "ROLE_FORBIDDEN", "NOT_FOUND", "AUTH_CONTEXT_CHANGED"].includes(code)) { revoked.current = true; setWorkspace(null); setSelected(null); source?.close(); if (timer) clearTimeout(timer); setError("当前项目访问权限已变化，请重新确认账号。"); if (code !== "NOT_FOUND") window.dispatchEvent(new Event(DESIGN_SESSION_INVALIDATED_EVENT)); } } catch { /* Network errors recover the stream, never tasks. */ } });
      source.onerror = () => { if (valid() && !revoked.current) { setConnection("实时连接中断，正在重连；原任务不会重新执行。"); reconnect(); } };
    }
    connect();
    const invalidate = () => { revoked.current = true; setWorkspace(null); setSelected(null); source?.close(); if (timer) clearTimeout(timer); };
    window.addEventListener(DESIGN_SESSION_INVALIDATED_EVENT, invalidate);
    return () => { disposed = true; alive.current = false; source?.close(); if (timer) clearTimeout(timer); window.removeEventListener(DESIGN_SESSION_INVALIDATED_EVENT, invalidate); };
  }, [projectId, retry, refresh, accept]);
  const dirty = canvas.dirty || !!canvas.pending || messageDirty;
  useEffect(() => { const guard = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); }; window.addEventListener("beforeunload", guard); return () => window.removeEventListener("beforeunload", guard); }, [dirty]);
  useEffect(() => {
    const guard = (event: MouseEvent) => {
      if (!dirty || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = (event.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!anchor || anchor.target === "_blank" || anchor.hasAttribute("download")) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.pathname === window.location.pathname && url.search === window.location.search) return;
      if (!window.confirm("仍有未保存内容或待核对操作。保留隔离草稿并离开项目？")) { event.preventDefault(); event.stopPropagation(); }
    };
    document.addEventListener("click", guard, true); return () => document.removeEventListener("click", guard, true);
  }, [dirty]);
  const statusNames: Record<string, string> = { loading: "正在读取画布", clean: "布局已同步", dirty: "布局待保存", saving: "正在保存布局", saved: "布局已保存", unknown: "原布局操作待核对", conflict: "布局待核对", error: "布局保存未完成" };
  return <main className={s.projectShell} data-tab={tab} data-chat={chatOpen}><header className={s.projectHeader}><Link className={s.backLink} href="/" aria-label="返回创作首页">←</Link><div className={s.projectTitle}><h1>{workspace?.project.name ?? `设计项目 ${projectId}`}</h1><span>{workspace?.project_context?.source_mode === "upstream" ? "上游定向" : workspace?.project_context?.source_mode === "independent" ? "自主设计" : "正在读取"} · {editable ? statusNames[canvas.state] : "只读 · 仅本次查看"}</span></div><div className={s.headerActions}>
    {canvasEditable && <button disabled={!canvas.dirty || canvas.state !== "dirty"} onClick={() => void canvas.save()}>保存布局</button>}
    {workspace?.source_binding && <button ref={sourceTrigger} aria-expanded={sourceOpen} onClick={() => setSourceOpen(value => !value)}>来源要求</button>}
    <Link href={workspace?.source_binding?.handoff_id ? `/handoffs?handoff=${encodeURIComponent(workspace.source_binding.handoff_id)}` : "/handoffs"}>{user.role === "manager" ? "收件与审查" : "任务管理"}</Link><button className={s.chatToggle} aria-pressed={chatOpen} onClick={() => setChatOpen(value => !value)}>对话</button></div></header>
    <div className={s.mobileTabs} role="tablist" aria-label="项目工作区视图"><button role="tab" aria-selected={tab === "canvas"} onClick={() => setTab("canvas")}>画布</button><button role="tab" aria-selected={tab === "chat"} onClick={() => { setChatOpen(true); setTab("chat"); }}>对话</button></div>
    {(error || connection) && <div className={s.connectionBar} role={error ? "alert" : "status"}>{error || connection}{error && <button onClick={() => setRetry(value => value + 1)}>重新读取项目</button>}</div>}
    {workspace ? <div className={s.workspaceBody}><DesignCanvas workspace={workspace} canvas={canvas} editable={canvasEditable} selected={selected} onSelect={setSelected} /><ProjectConversation workspace={workspace} user={user} selected={selected} onSelect={setSelected} editable={conversationEditable} caps={caps} refresh={refresh} onDirty={setMessageDirty} /></div> : <div className={s.projectLoading} role="status">{error ? "请重新确认项目访问权限。" : "正在恢复项目和历史…"}</div>}
    {workspace && (canvas.error || canvas.pending || canvas.state === "conflict") && <div className={s.canvasRecovery} role="status"><b>{statusNames[canvas.state]}</b><p>{canvas.error || "本地布局仍保留，请明确核对后继续。"}</p><div>{canvas.pending ? <><button onClick={() => void canvas.recover()}>查询原布局操作</button><button disabled={!canvasEditable || canvas.pending.context !== user.auth_context_id} onClick={() => void canvas.recover(true)}>重试原布局操作</button></> : <><button onClick={() => void canvas.reread()}>读取最新布局</button>{canvas.remote && <><button onClick={() => { if (window.confirm("放弃本地布局，使用已读取的服务端布局？")) canvas.adoptRemote(); }}>采用已保存布局</button><button disabled={!canvasEditable} onClick={canvas.rebase}>已核对，以本地布局继续保存</button></>}</>}</div></div>}
    {workspace && sourceOpen && <section className={s.sourceDrawer} aria-label="完整上游来源"><header><h2>完整来源与硬约束</h2><button aria-label="关闭来源" onClick={() => setSourceOpen(false)}>×</button></header>{workspace.source_binding && <SourceRequirements binding={workspace.source_binding} />}</section>}
  </main>;
}
