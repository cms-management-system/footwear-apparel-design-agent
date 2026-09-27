"use client";

import { useEffect, useMemo, useState } from "react";
import { agentApi, type ProjectCms } from "@/lib/agent-api";
import { suggestedDesignNote } from "@/lib/cms";
import styles from "./design.module.css";

const statuses = [
  { value: "adopted", label: "采纳" },
  { value: "not_adopted", label: "不采纳" },
  { value: "need_evidence", label: "需补证" },
] as const;

export default function CmsReturn({
  projectId,
  versionId,
  cms,
  intent,
  reviewSummary,
}: {
  projectId: number;
  versionId: string;
  cms?: ProjectCms | null;
  intent?: string;
  reviewSummary?: string;
}) {
  const pkg = cms?.package;
  const suggested = useMemo(() => suggestedDesignNote({
    requirementDesc: pkg?.requirement_desc,
    constraintsText: pkg?.constraints_text,
    intent,
    reviewSummary,
  }), [pkg?.requirement_desc, pkg?.constraints_text, intent, reviewSummary]);
  const [note, setNote] = useState(suggested);
  const [dirty, setDirty] = useState(false);
  const [status, setStatus] = useState<(typeof statuses)[number]["value"]>("adopted");
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{ design_version_id: string; cms_status: string } | null>(null);

  useEffect(() => {
    if (!dirty) setNote(suggested);
  }, [suggested, dirty]);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const saved = await agentApi.submitCmsResponse(projectId, { version_id: versionId, response_status: status, design_note: note.trim() });
      setResult({ design_version_id: saved.design_version_id, cms_status: saved.cms_status });
      setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "回传 CMS 没有完成，请稍后重试。");
      setOpen(false);
    } finally {
      setBusy(false);
    }
  }

  return <section className={styles.cmsReturn} aria-label="回传 CMS">
    <div className={styles.sampleHandoffHeading}>
      <div>
        <span className={styles.eyebrow}>写入 CMS</span>
        <h2>回传 CMS</h2>
        <p>把已确认的设计写回 CMS。CMS 会先存成草稿，由产品负责人稍后入档。</p>
      </div>
    </div>
    {!pkg?.package_id && <p className={styles.muted}>回传 CMS 需要先从对话入口导入已批准的证据包。</p>}
    {pkg?.package_id && <>
      <p className={styles.packageChip}>证据包 <span className={styles.mono}>{pkg.package_id}</span></p>
      <label>回传状态
        <select value={status} onChange={event => setStatus(event.target.value as (typeof statuses)[number]["value"])}>
          {statuses.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
      </label>
      <label>设计说明
        <textarea rows={5} maxLength={4000} value={note} onChange={event => { setNote(event.target.value); setDirty(true); }} />
      </label>
      <div className={styles.actions}>
        <button type="button" className={styles.primary} disabled={busy || !note.trim()} onClick={() => setOpen(true)}>回传 CMS</button>
      </div>
    </>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {result && <p role="status" className={styles.notice}>已写入 CMS。设计编号 <span className={styles.mono}>{result.design_version_id}</span>，状态 {result.cms_status}（待产品负责人入档）。</p>}
    {open && <div className={styles.editOverlay}>
      <div className={styles.editDialog} role="dialog" aria-modal="true" aria-labelledby="cms-return-title">
        <h2 id="cms-return-title">确认写入 CMS</h2>
        <p>确认后会把这款设计写入 CMS，状态为草稿，待产品负责人入档。同一设计编号再次提交不会重复创建记录。</p>
        <div className={styles.editDialogActions}>
          <button type="button" disabled={busy} onClick={() => setOpen(false)}>取消</button>
          <button type="button" className={styles.primary} disabled={busy} onClick={() => void submit()}>{busy ? "正在写入…" : "确认写入"}</button>
        </div>
      </div>
    </div>}
  </section>;
}
