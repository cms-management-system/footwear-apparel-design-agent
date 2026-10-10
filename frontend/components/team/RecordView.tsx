import { Fragment } from "react";
const names: Record<string, string> = { positive_prompt: "完整设计提示词", design_spec: "设计规格", requirement: "产品需求", prompt: "设计提示词", requirement_desc: "需求描述", constraints: "约束", avoid_items: "避免项", unknown_fields: "待补充信息", conflicts: "冲突", field_sources: "字段出处", human_additions: "人工补充", material: "面料", craft: "工艺", knowledge_status: "资料状态", source: "来源", source_refs: "来源引用", data_origin: "数据属性", approval: "批准记录", source_receipt: "技术接收回执", digest: "内容摘要", sha256: "SHA-256 摘要", version: "版本", revision: "修订", status: "状态", actor: "操作人", action: "动作", created_at: "记录时间", note: "说明", title: "标题", summary: "摘要", keywords: "关键词", scope: "作用域", source_instance_id: "来源实例", target_instance_id: "接收实例", presentation_rules: "展示要求", priority: "优先级", priority_basis: "优先级依据", spec: "设计规格", generation_mode: "生成方式" };
/** Text-only recursive rendering; payloads never become HTML or clickable untrusted URLs. */
export default function RecordView({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined) return <span className="recordUnknown">待补充</span>;
  if (typeof value !== "object") return <span className="recordText">{String(value)}</span>;
  if (depth > 12) return <pre className="recordText">{JSON.stringify(value, null, 2)}</pre>;
  if (Array.isArray(value)) return value.length ? <ul className="recordList">{value.map((entry, i) => <li key={i}><RecordView value={entry} depth={depth + 1} /></li>)}</ul> : <span className="recordUnknown">未列出</span>;
  return <dl className="recordFields">{Object.entries(value as Record<string, unknown>).map(([key, entry]) => <Fragment key={key}><dt>{names[key] ?? key}</dt><dd><RecordView value={entry} depth={depth + 1} /></dd></Fragment>)}</dl>;
}
