"use client";
import { appPath } from "@/lib/app-path";

import { useCallback, useEffect, useState } from "react";
import { agentApi, type Workspace } from "@/lib/agent-api";
import CmsReturn from "./CmsReturn";
import SamplingSheetPanel from "./SamplingSheetPanel";
import TechnicalFlatPanel from "./TechnicalFlatPanel";
import styles from "./design.module.css";

export default function SelectedDesignPage({ versionId, projectId }: { versionId: string; projectId: number }) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState("");
  const [dirty, setDirty] = useState(false);
  const validProject = Number.isSafeInteger(projectId) && projectId > 0;
  const back = appPath(validProject ? `/?project=${projectId}` : "/");
  const refresh = useCallback(async () => { setWorkspace(await agentApi.workspace(projectId)); }, [projectId]);

  useEffect(() => {
    if (!validProject) return;
    let cancelled = false;
    agentApi.workspace(projectId).then(data => { if (!cancelled) setWorkspace(data); })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : "无法读取方案，请返回设计对话重试。"); });
    return () => { cancelled = true; };
  }, [projectId, validProject]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  const version = workspace?.versions.find(item => item.id === versionId);
  const spec = workspace?.specs.find(item => item.id === version?.spec_id)?.spec;
  const title = version?.design_index ? `方案 ${version.design_index} / ${version.design_count}` : "选中的设计方案";
  const confirmed = !!version && version.status === "confirmed";
  const sheet = workspace?.sampling_sheets?.filter(item => item.version_id === versionId).at(-1);
  const flat = workspace?.technical_flats?.filter(item => item.version_id === versionId).at(-1);
  const requirements = spec?.constraints.filter(item => item.kind === "must_keep") ?? [];
  const hasFlatTemplate = /T\s*恤|T[- ]?shirt/i.test(spec?.intent ?? "");
  useEffect(() => {
    if (flat?.status !== "running") return;
    const timer = setInterval(() => { void refresh().catch(() => undefined); }, 3000);
    return () => clearInterval(timer);
  }, [flat?.status, refresh]);

  return <main className={`${styles.studio} ${styles.designPage}`}>
    <div className={styles.designPageTop}>
      <a href={back} onClick={event => { if (dirty && !window.confirm("打样细节尚未保存，离开后会丢失。继续？")) event.preventDefault(); }}>← 返回设计对话</a>
      <span>{confirmed ? "已选定方案" : "设计方案"}</span>
    </div>
    {!validProject && <p role="alert" className={styles.error}>链接缺少项目编号，请从设计对话重新打开这张图。</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {validProject && !workspace && !error && <p role="status">正在打开方案…</p>}
    {workspace && !version && <p role="alert" className={styles.error}>没有找到这张设计图。请返回设计对话重新选择。</p>}
    {version && <>
      <header className={styles.designPageHeading}>
        <div><span className={styles.eyebrow}>{workspace?.project.name}</span><h1>{title}</h1><p>{spec?.intent ?? "查看选中的设计图与设计要求。"}</p>
          {workspace?.project.cms?.package_id && <p className={styles.packageChip}>已关联证据包 <span className={styles.mono}>{workspace.project.cms.package_id}</span></p>}
        </div>
        <a className={styles.pageLink} href={agentApi.delivery(version.id)}>下载图片与设计资料</a>
      </header>
      <div className={styles.designPageHero}>
        <figure className={styles.designPageImage}>
          <img src={agentApi.image(version.id, "version")} alt={`${workspace?.project.name}的${title}效果图`} />
          <figcaption>选中的效果图 · {title}</figcaption>
        </figure>
        <aside className={styles.designPageBrief}>
          <span className={styles.eyebrow}>设计依据</span>
          <h2>这张图保留了什么</h2>
          {requirements.length ? <ul>{requirements.map(item => <li key={item.id}>{item.text}</li>)}</ul> : <p>{spec?.intent}</p>}
          {version.review?.summary && <details><summary>查看图片检查结果</summary><p>{version.review.summary}</p></details>}
          <div className={styles.designPageNext}>
            <h3>接下来</h3>
            <p>{confirmed ? "图片已选定。可直接下载交付资料；如需打样，再核对面料、尺寸和工艺。" : "确认这张图片后，才能继续整理对应的打样资料。"}</p>
            <a href={back}>返回对话{confirmed ? "修改这款" : "确认这款"} →</a>
          </div>
        </aside>
      </div>
      {confirmed && <CmsReturn projectId={projectId} versionId={version.id} cms={workspace?.project.cms} intent={spec?.intent} reviewSummary={version.review?.summary} />}
      {confirmed && <section className={styles.sampleHandoff} aria-label="首版打样交接">
        <div className={styles.sampleHandoffHeading}>
          <div><span className={styles.eyebrow}>图片已选定 · 下一步</span><h2>打样交接资料</h2><p>{sheet ? "这款的设计依据已整理。你可以下载沟通包，未知参数留待核对。" : "让助手先整理已确认的要求和图片检查结果，未知参数留待核对。"}</p></div>
          {sheet && <a className={styles.pageLink} href={agentApi.samplePack(version.id)}>下载打样沟通包</a>}
        </div>
        {!hasFlatTemplate && <TechnicalFlatPanel versionId={version.id} flat={flat} onSaved={refresh} />}
        <SamplingSheetPanel versionId={version.id} title={title} sheet={sheet} locked={false} onSaved={refresh} onDirtyChange={(_, value) => setDirty(value)} />
        {hasFlatTemplate ? <div className={styles.sampleFlats}>
          <figure><img src={agentApi.sampleFlat(version.id, "front")} alt="正面结构示意图" /><figcaption>正面结构示意 · 图案位置待核对</figcaption></figure>
          <figure><img src={agentApi.sampleFlat(version.id, "back")} alt="背面结构示意图" /><figcaption>背面结构示意 · 不可见细节待核对</figcaption></figure>
        </div> : <p className={styles.sampleCaveat}>{flat?.status === "confirmed" ? "已确认的正面 SVG 会随沟通包导出。背面、尺寸和裁片仍待核对。" : "确认上方草图后，正面 SVG 会加入沟通包；背面、尺寸和裁片仍待核对。"}</p>}
        {hasFlatTemplate && <p className={styles.sampleCaveat}>结构图不是纸样；面料、准确尺寸和工艺等未知项会在交接单中标为“待核对”，不会由效果图推断。</p>}
      </section>}
    </>}
  </main>;
}
