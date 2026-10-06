"use client";

import { useEffect, useRef, useState } from "react";
import { agentApi, type Capabilities, type Model3D, type Version } from "@/lib/agent-api";
import styles from "./design.module.css";

function RotatingModel({ src, poster, title }: { src: string; poster: string; title: string }) {
  const mount = useRef<HTMLDivElement>(null);
  const [error, setError] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let disposed = false;
    let viewer: HTMLElement | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setError(false); setLoaded(false);
    import("@google/model-viewer").then(() => {
      if (disposed || !mount.current) return;
      viewer = document.createElement("model-viewer");
      viewer.setAttribute("src", src);
      viewer.setAttribute("poster", poster);
      viewer.setAttribute("alt", `${title}的可旋转三维模型`);
      viewer.setAttribute("camera-controls", "");
      viewer.setAttribute("touch-action", "pan-y");
      viewer.setAttribute("shadow-intensity", "0.7");
      viewer.setAttribute("exposure", "1.1");
      viewer.className = styles.modelViewer;
      viewer.addEventListener("load", () => { if (!disposed) { setLoaded(true); if (timer) clearTimeout(timer); } });
      viewer.addEventListener("error", () => { if (!disposed) { setError(true); if (timer) clearTimeout(timer); } });
      mount.current.appendChild(viewer);
      timer = setTimeout(() => { if (!disposed) setError(true); }, 15000);
    }).catch(() => { if (!disposed) setError(true); });
    return () => { disposed = true; if (timer) clearTimeout(timer); viewer?.remove(); };
  }, [src, poster, title]);
  return <div className={styles.modelStage}>
    <div ref={mount} className={styles.modelMount} />
    {error ? <p role="alert">当前浏览器无法显示 3D，可下载 GLB 后在支持 WebGL 的浏览器查看。</p>
      : loaded ? <p className={styles.muted}>拖动旋转，滚轮或双指缩放。背面细节由模型推测，请与原图核对。</p>
      : <p role="status" className={styles.muted}>正在加载 3D 模型…</p>}
    <a href={src} download={`${title}.glb`}>下载 3D 模型（GLB）</a>
  </div>;
}

export default function Model3DPanel({ version, title, model, caps }: {
  version: Version; title: string; model?: Model3D; caps: Capabilities | null;
}) {
  const [view, setView] = useState<"image" | "model">("image");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const image = agentApi.image(version.id, "version");
  const ready = model?.status === "succeeded";
  const inProgress = !!model && ["queued", "submitting", "running", "saving"].includes(model.status);
  async function generate() {
    if (submitting || model || !caps?.three_d?.enabled) return;
    setSubmitting(true); setError("");
    try {
      const storage = `model3d-request:${version.id}`;
      let key = sessionStorage.getItem(storage);
      if (!key) { key = crypto.randomUUID(); sessionStorage.setItem(storage, key); }
      await agentApi.generate3D(version.id, key);
      sessionStorage.removeItem(storage);
    } catch (e) {
      setError(e instanceof Error ? e.message : "3D 任务未提交，请稍后重试。");
    } finally { setSubmitting(false); }
  }
  return <section className={styles.modelSection} aria-label={`${title}的效果图${model || caps?.three_d?.enabled ? "与3D预览" : ""}`}>
    {ready && <div className={styles.modelTabs} role="group" aria-label="查看方式">
      <button type="button" aria-pressed={view === "image"} onClick={() => setView("image")}>效果图</button>
      <button type="button" aria-pressed={view === "model"} onClick={() => setView("model")}>旋转查看 3D</button>
    </div>}
    {view === "model" && ready ? <RotatingModel src={agentApi.model3D(model.id)} poster={image} title={title} />
      : <a href={image} target="_blank" rel="noreferrer"><img className={styles.chatDesign} src={image} alt={title} /></a>}
    {!model && (caps?.three_d?.enabled ? <div className={styles.modelInvitation}>
      <button type="button" disabled={submitting || version.status === "candidate"} onClick={() => void generate()}>
        {submitting ? "正在提交 3D 任务…" : `制作这款的 3D 预览 · 预计 ¥${((caps?.three_d?.estimated_cost_fen ?? 180) / 100).toFixed(2)}`}
      </button>
      <p className={styles.muted}>仅发送这款效果图至火山方舟影眸；点击即提交一次付费生成。3D 是概念预览，需人工检查背面和结构。</p>
    </div> : null)}
    {inProgress && <p role="status" className={styles.modelProgress}>{model.status === "saving" ? "模型已生成，正在保存到本地…" : "正在为这款生成真正的 3D 模型；完成后可在这里旋转查看。"}</p>}
    {model?.status === "interrupted" && <p role="alert" className={styles.error}>提交结果不确定，已停止自动重试，避免重复计费。请核对方舟任务记录。</p>}
    {["failed", "failed_before_submit"].includes(model?.status ?? "") && <p role="alert" className={styles.error}>{model?.error?.message ?? "3D 生成失败；原效果图仍保留。"}</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </section>;
}
