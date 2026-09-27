"use client";

import { useState, type FormEvent } from "react";
import { agentApi, type CmsPackage } from "@/lib/agent-api";
import { requirementFromPackage } from "@/lib/cms";
import styles from "./design.module.css";

export default function CmsImport({ busy, onUse }: { busy: boolean; onUse: (prompt: string, packageId: string) => void }) {
  const [packageId, setPackageId] = useState("");
  const [pkg, setPkg] = useState<CmsPackage | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function load(event: FormEvent) {
    event.preventDefault();
    const id = packageId.trim();
    if (!id || loading) return;
    setLoading(true);
    setError("");
    setPkg(null);
    try {
      setPkg(await agentApi.cmsPackage(id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取证据包失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }

  return <section className={styles.cmsImport} aria-label="从 CMS 导入证据包">
    <h2>从 CMS 导入证据包</h2>
    <p className={styles.muted}>只读取产品负责人已批准的证据包，然后用其中的需求开始设计。</p>
    <form onSubmit={event => void load(event)}>
      <label>证据包编号
        <input value={packageId} maxLength={80} onChange={event => setPackageId(event.target.value)} placeholder="例如 PKG-20260927-0001" />
      </label>
      <div className={styles.actions}>
        <button type="submit" disabled={loading || !packageId.trim()}>{loading ? "正在读取…" : "读取证据包"}</button>
      </div>
    </form>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {pkg && <article className={styles.cmsCard} aria-label="证据包摘要">
      <h3>证据包 <span className={styles.mono}>{pkg.package_id}</span></h3>
      <p><b>需求描述</b>{pkg.requirement_desc || "（未填写）"}</p>
      <p><b>约束</b>{pkg.constraints_text || "（未填写）"}</p>
      <p><b>关联信号数</b>{pkg.signal_count}</p>
      <p><b>版本</b>{pkg.version ?? "—"}</p>
      {pkg.style_demand ? <p><b>风格诉求</b>{pkg.style_demand}</p> : null}
      <details>
        <summary>查看原始 JSON</summary>
        <pre className={styles.cmsRaw}>{JSON.stringify(pkg.raw, null, 2)}</pre>
      </details>
      <div className={styles.actions}>
        <button type="button" className={styles.primary} disabled={busy} onClick={() => onUse(requirementFromPackage(pkg), pkg.package_id)}>用这个需求开始设计</button>
      </div>
    </article>}
  </section>;
}
