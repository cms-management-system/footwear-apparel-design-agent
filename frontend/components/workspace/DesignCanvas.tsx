"use client";
import { useEffect, useRef, useState, type PointerEvent, type KeyboardEvent } from "react";
import { agentApi, type Workspace } from "@/lib/agent-api";
import { clamp, fitCanvas, zoomCanvas } from "@/lib/canvas-geometry";
import type { CanvasLayout, CanvasNode, SelectedContext } from "@/lib/workspace-api";
import type { useCanvas } from "./useCanvas";
import s from "./workspace.module.css";
export type CanvasController = ReturnType<typeof useCanvas>;
export default function DesignCanvas({ workspace, canvas, editable, selected, onSelect }: { workspace: Workspace; canvas: CanvasController; editable: boolean; selected: SelectedContext | null; onSelect: (value: SelectedContext | null) => void }) {
  const [tool, setTool] = useState<"select" | "hand">("select"); const [space, setSpace] = useState(false); const [library, setLibrary] = useState(false);
  const [failed, setFailed] = useState<string[]>([]); const surface = useRef<HTMLDivElement>(null);
  const libraryTrigger = useRef<HTMLButtonElement>(null);
  useEffect(() => { const element = surface.current; const preventPageZoom = (event: WheelEvent) => { if (event.ctrlKey || event.metaKey) event.preventDefault(); }; element?.addEventListener("wheel", preventPageZoom, { passive: false }); return () => element?.removeEventListener("wheel", preventPageZoom); }, []);
  useEffect(() => { if (!library) return; const escape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape" && (event.target as Element | null)?.closest?.("[data-project-library]")) { setLibrary(false); libraryTrigger.current?.focus(); } }; window.addEventListener("keydown", escape); return () => window.removeEventListener("keydown", escape); }, [library]);
  const drag = useRef<{ id?: string; x: number; y: number; original: CanvasLayout; pointer: number } | null>(null);
  const { layout } = canvas;
  function select(node: CanvasNode) { onSelect(node.kind === "version" ? { version_id: node.ref_id, asset_id: null } : { version_id: null, asset_id: node.ref_id }); }
  function start(event: PointerEvent<HTMLDivElement>, node?: CanvasNode) {
    if (!layout || event.button !== 0) return;
    const hand = tool === "hand" || space;
    if (node && !hand) select(node); else if (!node && !hand) onSelect(null);
    if (node && !hand && !editable) return;
    if (!node && !hand) return;
    event.preventDefault(); event.stopPropagation(); event.currentTarget.setPointerCapture?.(event.pointerId);
    canvas.gesture.current = true;
    drag.current = { id: hand ? undefined : node?.id, x: event.clientX, y: event.clientY, original: layout, pointer: event.pointerId };
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const d = drag.current; if (!d || event.pointerId !== d.pointer) return;
    const dx = event.clientX - d.x; const dy = event.clientY - d.y;
    canvas.change(d.id ? { ...d.original, nodes: d.original.nodes.map(n => n.id === d.id ? { ...n, x: clamp(n.x + dx / d.original.viewport.zoom, -100000, 100000), y: clamp(n.y + dy / d.original.viewport.zoom, -100000, 100000) } : n) } : { ...d.original, viewport: { ...d.original.viewport, x: clamp(d.original.viewport.x + dx, -100000, 100000), y: clamp(d.original.viewport.y + dy, -100000, 100000) } });
  }
  function end() { const wasDragging = !!drag.current; drag.current = null; canvas.gesture.current = false; if (wasDragging && canvas.dirty && layout) canvas.change({ ...layout }); }
  function fit() { if (layout && surface.current) canvas.change(fitCanvas(layout, surface.current.clientWidth, surface.current.clientHeight)); }
  function zoom(ratio: number) { if (layout && surface.current) canvas.change(zoomCanvas(layout, layout.viewport.zoom * ratio, { x: surface.current.clientWidth / 2, y: surface.current.clientHeight / 2 })); }
  function add(kind: "version" | "asset", id: string) {
    if (!layout || !editable || layout.nodes.length >= 200) return;
    const exists = layout.nodes.find(n => n.kind === kind && n.ref_id === id); if (exists) { select(exists); return; }
    const asset = kind === "asset" ? workspace.assets.find(a => a.id === id) : null;
    const width = 480; const height = asset && asset.width > 0 && asset.height > 0 ? clamp(480 * asset.height / asset.width, 24, 8192) : 480;
    const node: CanvasNode = { id: `node_${crypto.randomUUID()}`, kind, ref_id: id, x: 80 + layout.nodes.length * 72, y: 80 + layout.nodes.length * 48, width, height };
    canvas.change({ ...layout, nodes: [...layout.nodes, node] }); select(node); setLibrary(false);
  }
  function nodeKeys(event: KeyboardEvent<HTMLDivElement>, node: CanvasNode) {
    if (event.key === "Enter") { select(node); return; }
    if (!layout || !editable || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault(); const delta = event.shiftKey ? 40 : 8;
    canvas.change({ ...layout, nodes: layout.nodes.map(n => n.id !== node.id ? n : { ...n, x: clamp(n.x + (event.key === "ArrowLeft" ? -delta : event.key === "ArrowRight" ? delta : 0), -100000, 100000), y: clamp(n.y + (event.key === "ArrowUp" ? -delta : event.key === "ArrowDown" ? delta : 0), -100000, 100000) }) });
  }
  const versionIds = workspace.versions.map(v => v.id);
  return <section className={s.canvasArea} aria-label="项目画布">
    <div className={s.canvasTools} role="toolbar" aria-label="画布工具"><button aria-label="选择与移动图片" aria-pressed={tool === "select"} onClick={() => setTool("select")}><svg viewBox="0 0 24 24"><path d="m5 3 14 10-7 1-3 7-4-18Z" /></svg></button><button aria-label="平移画布" aria-pressed={tool === "hand"} onClick={() => setTool("hand")}><svg viewBox="0 0 24 24"><path d="M6 12V7a2 2 0 0 1 4 0v5-8a2 2 0 0 1 4 0v8-6a2 2 0 0 1 4 0v6-3a2 2 0 0 1 4 0v6c0 5-3 7-7 7H9l-6-7c-2-3 1-5 3-3Z" /></svg></button><span /> <button ref={libraryTrigger} aria-label="查看项目图片" aria-expanded={library} onClick={() => setLibrary(v => !v)}><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3" /><path d="m4 17 5-6 4 4 3-4 5 6M8 7h.1" /></svg></button></div>
    {/* The spatial editor needs a focusable application region; its image nodes and tools have independent keyboard actions. */}
    {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
    <div ref={surface} className={s.canvasSurface} role="application" data-tool={space ? "hand" : tool} tabIndex={0} aria-label="自由画布，空格平移，图片方向键移动" onPointerDown={e => start(e)} onPointerMove={move} onPointerUp={end} onPointerCancel={end} onKeyDown={e => { if (e.code === "Space") { e.preventDefault(); setSpace(true); } if (e.target === e.currentTarget && layout) { const arrows = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"]; if (arrows.includes(e.key)) { e.preventDefault(); canvas.change({ ...layout, viewport: { ...layout.viewport, x: clamp(layout.viewport.x + (e.key === "ArrowLeft" ? 40 : e.key === "ArrowRight" ? -40 : 0), -100000, 100000), y: clamp(layout.viewport.y + (e.key === "ArrowUp" ? 40 : e.key === "ArrowDown" ? -40 : 0), -100000, 100000) } }); } if (e.key === "0") fit(); if (e.key === "+" || e.key === "=") zoom(1.2); if (e.key === "-") zoom(1 / 1.2); if (e.key === "Escape") { onSelect(null); setLibrary(false); } } }} onKeyUp={e => { if (e.code === "Space") setSpace(false); }} onBlur={() => { setSpace(false); end(); }} onWheel={e => {
      if (!layout) return; const rect = e.currentTarget.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) canvas.change(zoomCanvas(layout, layout.viewport.zoom * Math.exp(-e.deltaY * .002), { x: e.clientX - rect.left, y: e.clientY - rect.top }));
      else canvas.change({ ...layout, viewport: { ...layout.viewport, x: clamp(layout.viewport.x - e.deltaX, -100000, 100000), y: clamp(layout.viewport.y - e.deltaY, -100000, 100000) } });
    }}>
      {layout && <div className={s.scene} style={{ transform: `translate(${layout.viewport.x}px,${layout.viewport.y}px) scale(${layout.viewport.zoom})` }}>{layout.nodes.map(node => <div role="button" tabIndex={0} aria-label={`画布${node.kind === "version" ? "版本" : "素材"} ${node.ref_id}`} aria-pressed={node.kind === "version" ? selected?.version_id === node.ref_id : selected?.asset_id === node.ref_id} key={node.id} className={s.imageNode} style={{ left: node.x, top: node.y, width: node.width, height: node.height }} onPointerDown={e => start(e, node)} onKeyDown={e => nodeKeys(e, node)} onClick={() => select(node)}>
        {failed.includes(node.id) ? <div className={s.imageFailure}>原图片暂不可读取</div> : <img src={agentApi.image(node.ref_id, node.kind)} alt={node.kind === "version" ? `鞋服设计版本 ${node.ref_id}` : `参考素材 ${node.ref_id}`} width={node.width} height={node.height} draggable={false} onError={() => setFailed(ids => [...ids, node.id])} />}
        <span className={s.nodeCaption}>{node.kind === "version" ? "设计版本" : "参考素材"} · {node.ref_id}</span>
      </div>)}</div>}
      {layout && !layout.nodes.length && <div className={s.canvasEmpty}><span className={s.canvasEmptyIcon}>＋</span><h2>给设计，留一张画布。</h2><p>{workspace.versions.length ? "已有设计保存在项目中，可以选择查看或加入画布。" : "从右侧说说你的想法。已保存的图片会在这里继续。"}</p><button onClick={() => setLibrary(true)}>查看项目图片</button></div>}
      {!layout && <div className={s.canvasEmpty}><p role="status">{canvas.state === "error" ? "画布暂不可读取" : "正在打开画布…"}</p><button onClick={() => void canvas.reread()}>重新读取画布</button></div>}
    </div>
    {library && <aside className={s.library} data-project-library aria-label="项目原图片"><header><h2>项目图片</h2><button aria-label="关闭项目图片" onClick={() => setLibrary(false)}>×</button></header>{!workspace.versions.length && !workspace.assets.length && <p className={s.muted}>项目暂无图片。描述和消息可继续保存。</p>}
      {versionIds.map(id => <article key={id}><button className={s.thumbButton} onClick={() => { onSelect({ version_id: id, asset_id: null }); }}><img src={agentApi.image(id, "version")} alt={`设计版本 ${id}`} width={120} height={120} loading="lazy" /></button><span>版本 {id}</span>{editable ? <button onClick={() => add("version", id)}>{layout?.nodes.some(n => n.kind === "version" && n.ref_id === id) ? "定位选择" : "加入画布"}</button> : <span className={s.muted}>只读查看</span>}</article>)}
      {workspace.assets.map(asset => <article key={asset.id}><button className={s.thumbButton} onClick={() => onSelect({ version_id: null, asset_id: asset.id })}><img src={agentApi.image(asset.id, "asset")} alt={asset.name} width={120} height={120} loading="lazy" /></button><span>{asset.name}</span>{editable && <button onClick={() => add("asset", asset.id)}>加入画布</button>}</article>)}
    </aside>}
    <div className={s.canvasBottom}><span className={s.canvasHint}>{editable ? "选中图片 · 拖动整理 · 空格平移" : "仅本次查看 · 布局不可保存"}</span><div className={s.zoomControls}><button aria-label="缩小画布" onClick={() => zoom(1 / 1.2)}>−</button><span>{Math.round((layout?.viewport.zoom ?? 1) * 100)}%</span><button aria-label="放大画布" onClick={() => zoom(1.2)}>＋</button><button onClick={fit}>适应画布</button></div></div>
  </section>;
}
