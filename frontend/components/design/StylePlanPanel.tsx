"use client";
import { useEffect, useState } from "react";
import { type StyleDirection, type StylePlan } from "@/lib/agent-api";
import styles from "./design.module.css";

export default function StylePlanPanel({ plan, locked, onDirtyChange, onSave, onConfirm }: {
  plan: StylePlan;
  locked: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onSave: (directions: StyleDirection[]) => Promise<boolean>;
  onConfirm: () => Promise<void>;
}) {
  const [directions, setDirections] = useState(plan.directions);
  const [saving, setSaving] = useState(false);
  const dirty = JSON.stringify(directions) !== JSON.stringify(plan.directions);
  const selected = directions.filter(direction => direction.selected).length;
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  function update(id: string, patch: Partial<StyleDirection>) {
    setDirections(current => current.map(direction => direction.id === id ? { ...direction, ...patch } : direction));
  }
  async function save() {
    setSaving(true);
    try { await onSave(directions); }
    finally { setSaving(false); }
  }
  return <section className={styles.stylePlan} aria-label="风格方向">
    <div className={styles.stylePlanHeading}><div><span className={styles.eyebrow}>设计助手的规划</span><h3>先看方向，再决定出图</h3></div><small>{selected} / {directions.length} 个方向保留</small></div>
    <div className={styles.styleDirections}>{directions.map((direction, index) => <article className={styles.styleDirection} key={direction.id} data-selected={direction.selected}>
      <div className={styles.styleDirectionTop}><span className={styles.directionIndex}>0{index + 1}</span><label><input type="checkbox" checked={direction.selected} disabled={locked || plan.status !== "draft"} onChange={event => update(direction.id, { selected: event.target.checked })} /> 保留这个方向</label></div>
      <h4>{direction.name}</h4><p>{direction.explore}</p><p className={styles.muted}>{direction.rationale}</p>
      <details><summary>{plan.status === "draft" ? "调整这条方向" : "查看完整方向"}</summary><div className={styles.styleDirectionDetails}>
        {plan.status === "draft" && <>
          <label>方向名称<input value={direction.name} disabled={locked} maxLength={60} onChange={event => update(direction.id, { name: event.target.value })} /></label>
          <label>主要设计变化<textarea value={direction.explore} disabled={locked} maxLength={300} rows={2} onChange={event => update(direction.id, { explore: event.target.value })} /></label>
          <label>为什么适合这次设计<textarea value={direction.rationale} disabled={locked} maxLength={500} rows={2} onChange={event => update(direction.id, { rationale: event.target.value })} /></label>
        </>}
        {([ ["设计主题", "theme"], ["配色参考", "color_story"], ["廓形与结构", "structure"], ["材料外观", "material_story"] ] as const).map(([label, field]) => plan.status === "draft"
          ? <label key={field}>{label}<textarea value={direction[field]} disabled={locked} rows={2} onChange={event => update(direction.id, { [field]: event.target.value })} /></label>
          : <p key={field}><b>{label}：</b>{direction[field]}</p>)}</div></details>
    </article>)}</div>
    {plan.status === "draft" && <div className={styles.stylePlanActions}>
      <button type="button" disabled={!dirty || locked || saving || !selected} onClick={() => void save()}>{saving ? "正在保存…" : "保存方向调整"}</button>
      <button type="button" className={styles.primary} disabled={dirty || locked || !selected} onClick={() => void onConfirm()}>确认这 {selected} 个方向</button>
      {dirty && <small>先保存调整，再确认方向。</small>}
    </div>}
  </section>;
}
