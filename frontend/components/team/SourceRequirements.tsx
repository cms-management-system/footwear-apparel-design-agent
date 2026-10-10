import RecordView from "./RecordView";
import s from "@/app/handoffs/handoff.module.css";

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
const hasContent = (value: unknown) => value !== null && value !== undefined && value !== "";

/** The decoded source is read-only; encoded transport copies are never displayed as content. */
export default function SourceRequirements({ binding }: { binding?: unknown }) {
  const source = record(binding);
  if (!source) return <section className={s.section} aria-label="上游完整需求"><h3>上游完整需求</h3><p className={s.subtle}>当前任务未提供完整上游来源绑定。请返回我的任务核对需求，现有摘要不能替代完整需求与设计提示词。</p></section>;
  const sourcePackage = record(source.package);
  const metadata = sourcePackage ? Object.fromEntries(Object.entries(sourcePackage).filter(([key]) => !["requirement_base64", "prompt_base64"].includes(key))) : null;
  return <section className={s.section} aria-label="上游完整需求">
    <h3>完整产品需求</h3>
    {hasContent(source.requirement) ? <RecordView value={source.requirement} /> : <p className={s.subtle}>完整产品需求尚未提供，请核对上游来源。</p>}
    <h3>完整设计提示词</h3>
    {hasContent(source.prompt) ? <RecordView value={source.prompt} /> : <p className={s.subtle}>完整设计提示词尚未提供，请核对上游来源。</p>}
    <p className={s.subtle}>以上为上游交接的只读正文。本地编辑和方案修改不会覆盖这份来源记录。</p>
    {metadata && Object.keys(metadata).length > 0 && <details className={s.section}><summary>来源包与版本信息</summary><RecordView value={metadata} /></details>}
    {hasContent(source.receipt) && <details className={s.section}><summary>技术接收回执</summary><RecordView value={source.receipt} /></details>}
  </section>;
}
