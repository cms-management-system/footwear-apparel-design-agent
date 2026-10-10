"use client";
import Link from "next/link";
import { agentApi, type CreationRun } from "@/lib/agent-api";
import type { SelectedContext } from "@/lib/workspace-api";
import { creationUnavailableMessage } from "@/lib/creation-copy";
import s from "./workspace.module.css";
const statuses: Record<CreationRun["status"], string> = { queued: "等待开始创作", running: "正在创作", succeeded: "已完成", needs_input: "需要你补充一下", blocked: "当前无法开始", failed: "创作未完成", unknown: "原请求结果待核对", superseded: "已有新的讨论，这次创作已停止", cancelled: "已取消" };
const stages: Record<CreationRun["stage"], string> = { text: "理解你的设计想法", freeze: "整理设计方案", image: "生成设计图片", canvas: "将图片放入画布", done: "完成" };
export function runInProgress(r: CreationRun) { return ["queued", "running", "unknown"].includes(r.status); }
export default function CreationRuns({ runs, projectId, onSelect, refresh, onError }: { runs: CreationRun[]; projectId: number; onSelect: (context: SelectedContext) => void; refresh: () => Promise<unknown>; onError: (cause: unknown) => void }) {
  return <>{runs.map(run => <article key={run.id} className={s.taskCard} aria-label="创作进度"><p role="status">{statuses[run.status] ?? run.status}{run.status === "running" ? ` · ${stages[run.stage] ?? run.stage}` : ""}</p>{run.reason && <p>{creationUnavailableMessage(run.reason)}</p>}{run.error?.message && <p className={s.error}>{run.error.message}</p>}{run.status === "unknown" && <p>先核对原请求；刷新不会重新调用模型。</p>}
    {run.version_id && <><button className={s.runImage} aria-label="查看本次生成图片" onClick={() => onSelect({ version_id: run.version_id!, asset_id: null })}><img src={agentApi.image(run.version_id, "version")} alt="本次任务实际生成的设计图" width={480} height={480} /></button><Link href={`/designs/${encodeURIComponent(run.version_id)}?project=${projectId}`}>查看版本与下载 →</Link>{run.canvas_placement !== "placed" && <p>图片已生成，待加入画布。可从项目图片中加入；无需重新生成。</p>}</>}
    <details><summary>创作记录</summary><p>任务 {run.id}</p>{run.steps?.map((step, i) => <p key={`${step.stage}-${i}`}>{stages[step.stage as CreationRun["stage"]] ?? step.stage} · {step.status}{step.response_model ? ` · ${step.response_model}` : step.request_model ? ` · 请求模型 ${step.request_model}` : step.model ? ` · ${step.model}` : ""}</p>)}</details><button onClick={() => void refresh().catch(onError)}>读取原创作进度</button>
  </article>)}</>;
}
