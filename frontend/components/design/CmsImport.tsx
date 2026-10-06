"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { agentApi, type CmsPackage } from "@/lib/agent-api";
import { requirementFromPackage } from "@/lib/cms";
import styles from "./design.module.css";

export default function CmsImport({ busy, onUse }: { busy: boolean; onUse: (prompt: string, packageId: string) => void }) {
  const [packages, setPackages] = useState<CmsPackage[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const mounted = useRef(false);
  const requestId = useRef(0);
  const inFlight = useRef(false);

  const load = useCallback(async (offset = 0) => {
    if (inFlight.current) return;
    inFlight.current = true;
    const id = ++requestId.current;
    setLoading(true);
    setError("");
    try {
      const result = await agentApi.cmsPackages(offset);
      if (!mounted.current || id !== requestId.current) return;
      setPackages(previous => Array.from(new Map((offset ? [...previous, ...result.items] : result.items).map(item => [item.package_id, item])).values()));
      setNextOffset(result.next_offset);
      setLoaded(true);
    } catch (reason) {
      if (!mounted.current || id !== requestId.current) return;
      setError(reason instanceof Error ? reason.message : "读取证据包失败，请稍后重试。");
    } finally {
      if (mounted.current && id === requestId.current) {
        inFlight.current = false;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const refresh = () => { if (document.visibilityState !== "hidden") void load(); };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    const timer = window.setInterval(refresh, 60000);
    return () => {
      mounted.current = false;
      inFlight.current = false;
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
      window.clearInterval(timer);
    };
  }, [load]);

  return <section className={styles.cmsImport} aria-label="从 CMS 导入证据包">
    <h2>从 CMS 导入证据包</h2>
    <p className={styles.muted}>自动同步当前空间已批准的需求。选择一项即可开始设计，无需填写编号。</p>
    <div className={styles.actions}>
      <button type="button" disabled={loading} onClick={() => void load()}>{loading ? "正在同步…" : error ? "重新同步" : "刷新需求"}</button>
      <a href={process.env.NEXT_PUBLIC_LINK_CMS || "http://localhost:8001"}>前往 CMS 审批</a>
    </div>
    {loading && !loaded && <p role="status">正在从 CMS 获取已批准的证据包…</p>}
    {loaded && !loading && !error && packages.length === 0 && <p role="status">当前空间暂无已批准的证据包。请先在 CMS 由产品负责人批准需求，返回这里后会自动同步。</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {packages.map(pkg => <article key={pkg.package_id} className={styles.cmsCard} aria-label="证据包摘要">
      <h3>{pkg.requirement_desc || "已批准的设计需求"}</h3>
      <p className={styles.muted}>已批准 · <span className={styles.mono}>{pkg.package_id}</span></p>
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
    </article>)}
    {nextOffset !== null && <div className={styles.actions}><button type="button" disabled={loading} onClick={() => void load(nextOffset)}>加载更多需求</button></div>}
  </section>;
}
