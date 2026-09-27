import type { CmsPackage } from "@/lib/agent-api";

export function requirementFromPackage(pkg: CmsPackage): string {
  const lines = [`请根据已批准的 CMS 证据包 ${pkg.package_id} 开始设计。`];
  if (pkg.requirement_desc) lines.push(`需求描述：${pkg.requirement_desc}`);
  if (pkg.style_demand) lines.push(`风格诉求：${pkg.style_demand}`);
  if (pkg.constraints_text) lines.push(`约束：\n${pkg.constraints_text}`);
  lines.push("请先整理设计要求，并和我确认后再出图。");
  return lines.join("\n");
}

export function suggestedDesignNote(input: { requirementDesc?: string; constraintsText?: string; intent?: string; reviewSummary?: string }): string {
  const lines: string[] = [];
  if (input.requirementDesc) lines.push(`本设计回应需求：${input.requirementDesc}`);
  if (input.constraintsText) lines.push(`约束处理：${input.constraintsText}`);
  if (input.intent) lines.push(`设计说明：${input.intent}`);
  if (input.reviewSummary) lines.push(`图片检查：${input.reviewSummary}`);
  return lines.join("\n") || "设计方案已确认，详见效果图。";
}
