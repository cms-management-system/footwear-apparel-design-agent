"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, currentDesignUser, errorText, type TeamUser } from "@/lib/team-api";
import { canvasStorageKey, freezeLayout, pendingLayout, queryLayout, workspaceApi, writeLayout, type CanvasDocument, type CanvasLayout, type LayoutAction } from "@/lib/workspace-api";

export function useCanvas(pid: number, user: TeamUser, editable: boolean, placementKey = "") {
  const [doc, setDoc] = useState<CanvasDocument | null>(null); const [layout, setLayout] = useState<CanvasLayout | null>(null);
  const [state, setState] = useState("loading"); const [error, setError] = useState(""); const [dirty, setDirty] = useState(false); const [pending, setPending] = useState<LayoutAction | null>(null); const [remote, setRemote] = useState<CanvasDocument | null>(null);
  const current = useRef({ layout, doc, dirty, editable, user, state }); current.current = { layout, doc, dirty, editable, user, state };
  const alive = useRef(true); const lock = useRef(false); const generation = useRef(0); const gesture = useRef(false);
  const latestRemote = useRef<CanvasDocument | null>(null);
  const observedRevision = useRef(-1);
  const placementSeen = useRef(placementKey);
  const initializedKey = useRef<string | null>(null);
  const key = `${canvasStorageKey(pid, user)}:draft`;
  useEffect(() => {
    alive.current = true; let active = true;
    const action = pendingLayout(pid, user); setPending(action);
    if (initializedKey.current !== key) {
      initializedKey.current = key;
      let draft: CanvasLayout | null = null;
      try { const saved = JSON.parse(sessionStorage.getItem(key) ?? "null"); if (saved?.schema_version === "design-canvas/1" && Array.isArray(saved.layout?.nodes) && saved.layout.viewport) draft = saved.layout; } catch { /* Saved private drafts are optional. */ }
      // An in-flight original layout remains usable even if its separate editing draft is absent.
      if (!draft && action) {
        try { const original = JSON.parse(action.body); if (Array.isArray(original.layout?.nodes) && original.layout.viewport) draft = original.layout; } catch { /* Keep unknown recovery without inventing a layout. */ }
      }
      const restoredState = action ? "unknown" : draft ? "conflict" : "loading";
      // Effects and fast GET promises can run before React commits these states: seed the guard too.
      current.current = { ...current.current, layout: draft, doc: null, dirty: !!draft, state: restoredState };
      observedRevision.current = -1; latestRemote.current = null;
      setDoc(null); setRemote(null); setLayout(draft); setDirty(!!draft); setState(restoredState);
    }
    workspaceApi.canvas(pid).then(value => {
      if (!active || currentDesignUser() !== user || value.layout_revision < observedRevision.current || latestRemote.current) return;
      observedRevision.current = value.layout_revision; latestRemote.current = value;
      setDoc(value); setRemote(value);
      const c = current.current; const original = pendingLayout(pid, user);
      if (!c.dirty && !original && !lock.current) { setLayout(value.layout); setState("clean"); }
      else setState(original ? "unknown" : "conflict");
    }).catch(cause => { if (active && observedRevision.current < 0) { setState("error"); setError(errorText(cause)); } });
    return () => { active = false; alive.current = false; };
  }, [pid, user, key]);
  const change = useCallback((value: CanvasLayout, persisted = true) => {
    setLayout(value); generation.current += 1;
    if (current.current.editable && persisted) {
      setDirty(true); setState(previous => ["unknown", "conflict", "saving"].includes(previous) ? previous : "dirty");
      try { sessionStorage.setItem(key, JSON.stringify({ schema_version: "design-canvas/1", layout: value })); } catch { /* In-memory draft remains. */ }
    }
  }, [key]);
  const save = useCallback(async () => {
    const c = current.current;
    if (lock.current || gesture.current || !c.editable || !c.doc || !c.layout || !c.dirty || ["unknown", "conflict", "error", "loading"].includes(c.state) || currentDesignUser() !== user) return;
    lock.current = true; setState("saving"); setError(""); const gen = generation.current;
    try {
      const action = freezeLayout(pid, c.doc, c.layout, user); setPending(action);
      const saved = await writeLayout(action, user);
      if (!alive.current || currentDesignUser() !== user) return;
      setPending(null);
      if (saved.layout_revision < observedRevision.current) { setRemote(latestRemote.current); setDirty(true); setState("conflict"); setError("新生成图片已加入服务端画布；本地布局仍保留，请核对后继续。"); return; }
      observedRevision.current = saved.layout_revision; latestRemote.current = saved; setDoc(saved);
      if (generation.current === gen) { setDirty(false); setState("saved"); sessionStorage.removeItem(key); } else setState("dirty");
    } catch (cause) {
      if (!alive.current) return; setError(errorText(cause)); const original = pendingLayout(pid, user); setPending(original);
      if (original) setState("unknown");
      else { setState(cause instanceof ApiError && cause.status === 409 ? "conflict" : "error"); if (cause instanceof ApiError && cause.status === 409) workspaceApi.canvas(pid).then(value => { if (alive.current && currentDesignUser() === user && value.layout_revision >= observedRevision.current) { observedRevision.current = value.layout_revision; latestRemote.current = value; setRemote(value); } }).catch(() => {}); }
    } finally { lock.current = false; }
  }, [pid, user, key]);
  useEffect(() => {
    if (!dirty || !editable || state !== "dirty") return;
    const timer = setTimeout(() => void save(), 900); return () => clearTimeout(timer);
  }, [layout, dirty, editable, state, save]);
  useEffect(() => { const guard = (e: BeforeUnloadEvent) => { if (dirty || pending) e.preventDefault(); }; window.addEventListener("beforeunload", guard); return () => window.removeEventListener("beforeunload", guard); }, [dirty, pending]);
  async function recover(retry = false) {
    if (!pending || lock.current || (retry && (!editable || pending.context !== user.auth_context_id))) return;
    lock.current = true; setError(""); const original = pending;
    try {
      const saved = retry ? await writeLayout(original, user) : await queryLayout(original, user);
      if (!alive.current || currentDesignUser() !== user) return;
      setPending(null);
      if (saved.layout_revision < observedRevision.current) { setRemote(latestRemote.current); setDirty(true); setState("conflict"); setError("原操作已确认；服务端已有更新的画布，本地稿仍保留，请核对。"); return; }
      observedRevision.current = saved.layout_revision; latestRemote.current = saved; setDoc(saved); setRemote(saved);
      const same = JSON.stringify(current.current.layout) === JSON.stringify(saved.layout);
      setDirty(!same); setState(same ? "saved" : "conflict");
      if (same) sessionStorage.removeItem(key);
    } catch (cause) { if (alive.current) { setError(errorText(cause)); setPending(pendingLayout(pid, user)); if (!pendingLayout(pid, user)) setState("conflict"); } }
    finally { lock.current = false; }
  }
  const reread = useCallback(async () => {
    try {
      const value = await workspaceApi.canvas(pid);
      if (!alive.current || currentDesignUser() !== user) return;
      const c = current.current;
      if (value.layout_revision < observedRevision.current) return;
      observedRevision.current = value.layout_revision; latestRemote.current = value; setRemote(value);
      if (!c.dirty && !pendingLayout(pid, user) && !lock.current) { setDoc(value); setLayout(value.layout); setState("clean"); setError(""); }
      else if (value.layout_revision > (c.doc?.layout_revision ?? 0)) { setState(pendingLayout(pid, user) ? "unknown" : "conflict"); setError("服务端画布已更新，本地编辑仍保留。请核对新生成图片与布局。"); }
    } catch (cause) { if (alive.current && currentDesignUser() === user) { setError(errorText(cause)); setState(pendingLayout(pid, user) ? "unknown" : "error"); } }
  }, [pid, user]);
  useEffect(() => {
    if (placementSeen.current === placementKey) return;
    placementSeen.current = placementKey;
    // Pause the debounce immediately; a server placement must not be overwritten by our old snapshot.
    if (current.current.dirty && !pendingLayout(pid, user)) setState("conflict");
    void reread();
  }, [placementKey, pid, user, reread]);
  function adoptRemote() { if (!remote || pending || lock.current) return; setDoc(remote); setLayout(remote.layout); setDirty(false); setState("clean"); setError(""); sessionStorage.removeItem(key); }
  function rebase() {
    if (!remote || pending || lock.current || !editable || !layout) return;
    const additions = remote.layout.nodes.filter(node => node.id.startsWith("generated_") && !layout.nodes.some(local => local.kind === node.kind && local.ref_id === node.ref_id));
    if (layout.nodes.length + additions.length > 200) { setError("保留新生成图片会超过画布上限，请先核对并整理节点。"); return; }
    const rebased = { ...layout, nodes: [...layout.nodes, ...additions] };
    setDoc(remote); setLayout(rebased); setDirty(true); setState("dirty"); setError("");
    try { sessionStorage.setItem(key, JSON.stringify({ schema_version: "design-canvas/1", layout: rebased })); } catch { /* In-memory draft remains. */ }
  }
  return { doc, layout, state, error, dirty, pending, remote, change, save, recover, reread, adoptRemote, rebase, gesture };
}
