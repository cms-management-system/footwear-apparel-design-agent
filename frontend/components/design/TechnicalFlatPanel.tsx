"use client";

import { useEffect, useRef, useState } from "react";
import { agentApi, type Capabilities, type TechnicalFlat } from "@/lib/agent-api";
import styles from "./design.module.css";

type FlatPanelProps = { versionId: string; flat?: TechnicalFlat; onSaved: () => Promise<void>; readOnly?: boolean; caps?: Capabilities | null };
export default function TechnicalFlatPanel(props: FlatPanelProps) {
  return <VersionFlatPanel key={`${props.versionId}:${!!props.readOnly}`} {...props} />;
}
function VersionFlatPanel({ versionId, flat, onSaved, readOnly = false, caps }: FlatPanelProps) {
  const [working, setWorking] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const [notice, setNotice] = useState("");
  async function run(action: () => Promise<TechnicalFlat>) {
    if (readOnly || working) return;
    setWorking(true); setError(""); setNotice("");
    try {
      const result = await action();
      if (!alive.current) return;
      try {
        await onSaved();
      } catch {
        setNotice("操作结果已保存，但页面暂未同步。请刷新页面查看最新状态。");
      }
      if (!alive.current) return;
      if (result.status === "failed") setError(result.error || "模型没有完成草图，请手动重试。");
      else if (result.status === "unsupported") setError("这张图的品类暂不支持生成可编辑技术图。");
      else if (result.status === "running") setNotice("任务正在进行，页面会继续同步结果。");
      else setFeedback("");
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : "操作未完成，请刷新查看状态。");
    } finally { if (alive.current) setWorking(false); }
  }
  const ready = flat?.status === "draft" || flat?.status === "confirmed";
  return <section className={styles.technicalFlatPanel} aria-label="技术平面图草稿">
    <div className={styles.technicalFlatHeading}>
      <div><span className={styles.eyebrow}>智能体绘图</span><h3>正面技术平面图</h3><p>依据已选效果图提取可见结构，生成可编辑 SVG。请核对轮廓与细节后再加入交接包。</p></div>
      {!flat && <button type="button" className={styles.primary} disabled={readOnly || working || !caps?.understand} onClick={() => void run(() => agentApi.generateTechnicalFlat(versionId))}>{working ? "正在识别并绘制…" : "生成草图"}</button>}
      {flat && ["failed", "interrupted"].includes(flat.status) && <button type="button" disabled={readOnly || working || !caps?.understand} onClick={() => void run(() => agentApi.generateTechnicalFlat(versionId))}>{working ? "正在重新生成…" : "手动重新生成"}</button>}
    </div>
    {!caps?.understand && <p className={styles.muted}>技术平面图生成服务未启用，当前只能查看已有草图。</p>}
    {(working || flat?.status === "running") && <p role="status" className={styles.notice}>正在分析这张图的正面可见结构；不会自动重复调用模型。</p>}
    {(error || flat?.error) && <p role="alert" className={styles.error}>{error || flat?.error}</p>}
    {flat?.status === "unsupported" && <p className={styles.notice}>这张图的品类暂不支持自动生成可编辑技术图。</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {ready && flat && <>
      <div className={styles.technicalFlatBody}>
        <img src={agentApi.technicalFlatSvg(flat.id)} alt="由效果图生成的正面技术平面图草稿" />
        <div>
          <p><b>{flat.status === "confirmed" ? "已确认并加入交接包" : "待你核对"}</b></p>
          {!!flat.features?.visible_details.length && <><h4>图中可见</h4><ul>{flat.features.visible_details.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
          {!!flat.features?.uncertain_details.length && <><h4>仍需核对</h4><ul>{flat.features.uncertain_details.map((item, index) => <li key={index}>{item}</li>)}</ul></>}
          <p className={styles.muted}>只绘制正面；背面、尺寸和裁片不能由这张效果图确定。</p>
          <div className={styles.actions}>
            {flat.status === "draft" && <button type="button" className={styles.primary} disabled={readOnly || working} onClick={() => void run(() => agentApi.confirmTechnicalFlat(flat.id))}>确认草图并加入交接包</button>}
            <a href={agentApi.technicalFlatSvg(flat.id)} download={`front-flat-${flat.id.slice(0, 8)}.svg`}>下载可编辑 SVG</a>
          </div>
        </div>
      </div>
      <label className={styles.technicalFlatFeedback}>需要调整图稿？直接描述
        <textarea value={feedback} onChange={event => setFeedback(event.target.value)} maxLength={1000} rows={2} placeholder="例如：保留衬衫领和门襟，把腰带改成腰部分割线。" disabled={readOnly || working || !caps?.understand} />
      </label>
      <button type="button" disabled={readOnly || working || !caps?.understand || !feedback.trim()} onClick={() => void run(() => agentApi.reviseTechnicalFlat(flat.id, feedback.trim()))}>生成修改版</button>
    </>}
  </section>;
}
