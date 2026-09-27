"use client";
import { useEffect, useRef, useState } from "react";
import type { Capabilities, Reference } from "@/lib/agent-api";
import styles from "./design.module.css";
export type StartAsset = { id: string; file: File; role: Reference["role"] };
export type StartInput = { intent: string; assets: StartAsset[]; authorized: boolean };
const roles = { structure: "草图 / 结构廓形", fabric: "面料外观", color: "配色参考", detail: "局部细节" };
function Preview({ file }: { file: File }) {
  const [url, setUrl] = useState("");
  useEffect(() => { const value = URL.createObjectURL(file); setUrl(value); return () => URL.revokeObjectURL(value); }, [file]);
  return url ? <img src={url} alt={`待上传：${file.name}`} /> : null;
}
export default function DesignStart({ caps, busy, onStart, onDirty, conversation = false, waitingForReply = false, attachmentBlocked = false, sendBlocked = false }: {
  sendBlocked?: boolean; conversation?: boolean; waitingForReply?: boolean; attachmentBlocked?: boolean; caps: Capabilities | null; busy: boolean; onStart: (input: StartInput) => void; onDirty: () => void;
}) {
  const [intent, setIntent] = useState("");
  const [assets, setAssets] = useState<StartAsset[]>([]);
  const [error, setError] = useState("");
  const composing = useRef(false);
  const changed = onDirty;
  function submit() {
    if (busy || sendBlocked || !caps || !intent.trim()) return;
    // Retained for the older /design form; the conversational backend no longer applies it.
    onStart({ intent: intent.trim(), assets, authorized: caps.understand && (!waitingForReply || assets.length > 0) });
  }
  return <section className={styles.start} aria-label="开始设计">
    <form onSubmit={e => {
      e.preventDefault();
      submit();
    }}>
      <fieldset disabled={busy} className={styles.startFields}>
        <label className={styles.intentLabel}>{conversation ? "发消息给设计助手" : "说说你要做什么样的衣服"}
          <textarea rows={2} maxLength={4000} value={intent} onChange={e => { setIntent(e.target.value); changed(); }} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={e => {
            if (e.key !== "Enter" || e.shiftKey || composing.current || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
            e.preventDefault();
            submit();
          }} placeholder={conversation ? "继续说说你的想法，或回答助手的问题…" : "例如：想做一条方领连衣裙，保留草图的腰线，把裙摆加长。"} />
        </label>
        <label className={styles.attach} aria-disabled={attachmentBlocked} title={attachmentBlocked ? "这轮出图已锁定参考图，请结束本轮后添加" : undefined}>＋ 参考图
          <input disabled={attachmentBlocked} className={styles.fileInput} aria-label="添加草图、面料照片或参考图（可选）" type="file" multiple accept="image/png,image/jpeg,image/webp" onChange={e => {
            const files = Array.from(e.target.files ?? []); e.target.value = "";
            if (!files.length) return;
            if (assets.length + files.length > 6) { setError("最多添加 6 张参考图，请先移除不需要的图片。"); return; }
            if (files.some(f => f.size > 10 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(f.type))) { setError("请选择 PNG、JPEG 或 WebP，每张不超过 10 MB。"); return; }
            setAssets(items => [...items, ...files.map(file => ({ id: crypto.randomUUID(), file, role: "structure" as const }))]); setError(""); changed();
          }} />

        </label>
        {error && <p role="alert" className={styles.error}>{error}</p>}
        <div className={styles.startAssets}>{assets.map(asset => <div key={asset.id} className={styles.asset}>
          <Preview file={asset.file} /><div><b>{asset.file.name}</b><label>图片用途<select value={asset.role} onChange={e => { setAssets(items => items.map(a => a.id === asset.id ? { ...a, role: e.target.value as Reference["role"] } : a)); changed(); }}>{Object.entries(roles).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
            <button type="button" aria-label={`移除 ${asset.file.name}`} onClick={() => { setAssets(items => items.filter(a => a.id !== asset.id)); changed(); }}>移除图片</button>
          </div></div>)}</div>
        {caps && !caps.understand && !waitingForReply && <p className={styles.muted}>视觉服务待开通，目前仅保存需求和素材。</p>}
        <div className={styles.actions}><button className={styles.primary} type="submit" disabled={busy || sendBlocked || !caps || !intent.trim()}>{busy ? "正在发送…" : conversation ? "发送" : "开始理解"}</button></div>
      </fieldset>
    </form>

  </section>;
}
