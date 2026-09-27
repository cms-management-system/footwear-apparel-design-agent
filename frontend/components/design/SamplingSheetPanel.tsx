"use client";
import { useEffect, useState, type FormEvent } from "react";
import { agentApi, blankSamplingFields, type SamplingFields, type SamplingSheet } from "@/lib/agent-api";
import styles from "./design.module.css";

const fields: { key: keyof SamplingFields; label: string; hint: string; wide?: boolean }[] = [
  { key: "material", label: "面料与辅料", hint: "例如面料成分、克重、辅料规格。以实际选样为准。" },
  { key: "color", label: "颜色与色号", hint: "写明主色、辅色及色卡编号；未知可留空。" },
  { key: "measurements", label: "尺码与关键尺寸", hint: "写明基准尺码和具体部位尺寸、单位及允差。", wide: true },
  { key: "graphic_placement", label: "图案或装饰位置", hint: "例如距领口、侧缝的距离；无图案可填“不适用”。" },
  { key: "graphic_dimensions", label: "图案或装饰尺寸", hint: "写明宽高及单位；无图案可填“不适用”。" },
  { key: "construction", label: "制作工艺", hint: "例如印花、绣花、缝制、边缘处理和关键做法。", wide: true },
  { key: "notes", label: "其他打样说明", hint: "补充版型、手感或需实物核对的要求。", wide: true },
];

export default function SamplingSheetPanel({ versionId, title, sheet, locked, onSaved, onDirtyChange }: {
  versionId: string; title: string; sheet?: SamplingSheet; locked: boolean;
  onSaved: () => Promise<void>; onDirtyChange: (id: string, dirty: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<SamplingFields>(() => sheet?.fields ?? blankSamplingFields());
  const [baseline, setBaseline] = useState<SamplingFields>(() => sheet?.fields ?? blankSamplingFields());
  const [sheetId, setSheetId] = useState<string | null>(sheet?.id ?? null);
  const [basis, setBasis] = useState(sheet?.basis);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const dirty = JSON.stringify(values) !== JSON.stringify(baseline);
  useEffect(() => {
    if (sheet && !dirty && sheet.id !== sheetId) {
      setValues(sheet.fields); setBaseline(sheet.fields); setSheetId(sheet.id); setBasis(sheet.basis);
    }
  }, [sheet, dirty, sheetId]);
  useEffect(() => { onDirtyChange(`sample:${versionId}`, dirty); return () => onDirtyChange(`sample:${versionId}`, false); }, [versionId, dirty, onDirtyChange]);
  const missing = fields.filter(field => field.key !== "notes" && !values[field.key].trim()).map(field => field.label);
  async function autoDraft() {
    if (saving || locked || sheetId || dirty) return;
    setSaving(true); setNotice(""); setError("");
    try {
      const generated = await agentApi.autoSamplingSheet(versionId);
      setSheetId(generated.id); setBaseline(generated.fields); setValues(generated.fields); setBasis(generated.basis);
      setNotice("已从这款的设计要求和检查结果整理草稿。你可以直接下载，也可以补充细节。");
      try { await onSaved(); } catch { setNotice("草稿已保存，页面同步稍慢，刷新后可继续查看。"); }
    } catch (e) { setError(e instanceof Error ? e.message : "整理失败，请重试。"); }
    finally { setSaving(false); }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving || locked || !dirty) return;
    setSaving(true); setNotice(""); setError("");
    try {
      const saved = await agentApi.saveSamplingSheet(versionId, values, sheetId);
      setSheetId(saved.id); setBaseline(saved.fields); setValues(saved.fields); setBasis(saved.basis);
      setNotice("打样资料草稿已保存，可继续补充或下载核对。");
      try { await onSaved(); } catch { setNotice("打样资料已保存，页面同步稍慢，刷新后可继续查看。"); }
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败，请重试。"); }
    finally { setSaving(false); }
  }
  return <section className={styles.samplingPanel} aria-label={`${title}的打样准备`}>
    <div className={styles.samplingIntro}>
      <div><span className={styles.eyebrow}>设计资料</span><h3>{sheetId ? "交接草稿" : "让助手先整理草稿"}</h3><p>沿用已确认的设计要求与图片检查结果。面料、准确尺寸等未知信息留待核对。</p></div>
      <div className={styles.samplingIntroActions}>
        {!sheetId && <button type="button" className={styles.primary} disabled={saving || locked || dirty} onClick={() => void autoDraft()}>{saving ? "正在整理…" : "自动整理草稿"}</button>}
        <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "收起细节" : sheetId ? "按需补充细节" : "自己补充细节"}</button>
      </div>
    </div>
    {sheetId && <div className={styles.samplingDigest}>
      <p><b>已整理的设计依据</b>　{basis?.intent ?? "已关联这款的设计图片和要求单。"}</p>
      {!!basis?.requirements.length && <p className={styles.muted}>已确认：{basis.requirements.join("；")}</p>}
      <p className={styles.muted}>{missing.length ? `待核对：${missing.join("、")}` : "主要参数已有记录，仍需与实物和打样方核对。"}</p>
      <p className={styles.samplingStatus}>{dirty ? "有未保存修改，请展开保存" : <>草稿已保存　<a href={agentApi.delivery(versionId)}>下载图片与打样资料</a></>}</p>
    </div>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}
    {open && <form onSubmit={event => void save(event)}>
      <div className={styles.samplingGrid}>{fields.map(field => <label key={field.key} className={field.wide ? styles.samplingWide : undefined}>{field.label}
        <span className={styles.samplingHint}>{field.hint}</span>
        <textarea value={values[field.key]} maxLength={field.key === "measurements" || field.key === "construction" || field.key === "notes" ? 3000 : 1200} rows={field.wide ? 3 : 2} disabled={saving} onChange={event => setValues(current => ({ ...current, [field.key]: event.target.value }))} />
      </label>)}</div>
      <p className={styles.muted}>只补充你已经确定的信息。空白项目会在导出文件中标为“待核对”。</p>
      <div className={styles.actions}><button type="submit" className={styles.primary} disabled={saving || locked || !dirty}>{saving ? "正在保存…" : "保存打样资料草稿"}</button></div>
    </form>}
    <p className={styles.muted}>草稿仍需实物、版型和工艺核对，不等于正式生产工艺单。</p>
  </section>;
}
