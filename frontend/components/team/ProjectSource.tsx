"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { workflowApi } from "@/lib/design-workflow";
import { errorText, isAbort } from "@/lib/team-api";
import SourceRequirements from "./SourceRequirements";
import s from "@/app/handoffs/handoff.module.css";

type SourceState = { status: "loading" } | { status: "missing" } | { status: "error"; message: string } | { status: "ready"; binding: unknown };

export default function ProjectSource({ projectId }: { projectId: number | null }) {
  if (projectId === null || !Number.isSafeInteger(projectId) || projectId <= 0) return <section className={s.section}><h3>上游需求与设计提示词</h3><p className={s.subtle}>从我的任务打开设计项目，即可查看对应的完整上游要求。</p><Link href="/handoffs">返回我的任务 →</Link></section>;
  // A project switch immediately removes the old source before the next read starts.
  return <ProjectSourceRead key={projectId} projectId={projectId} />;
}

function ProjectSourceRead({ projectId }: { projectId: number }) {
  const [state, setState] = useState<SourceState>({ status: "loading" });
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setState({ status: "loading" });
    async function read() {
      let offset = 0;
      const seen = new Set<string>();
      while (current && !controller.signal.aborted) {
        const page = await workflowApi.list(offset, controller.signal);
        if (!current || controller.signal.aborted) return;
        if (!Array.isArray(page.items) || !Number.isSafeInteger(page.total) || page.total < 0) throw new Error("任务列表格式异常，请重新读取上游要求。");
        const match = page.items.find(item => item.project_id === projectId && typeof item.id === "string" && !!item.id.trim());
        if (match) {
          const detail = await workflowApi.detail(match.id, controller.signal);
          if (!current || controller.signal.aborted) return;
          if (detail.id !== match.id || detail.project_id !== projectId) throw new Error("需求与当前设计项目不一致，未显示上游正文。请返回我的任务重新打开。");
          const binding: unknown = "source_binding" in detail ? detail.source_binding : undefined;
          setState({ status: "ready", binding });
          return;
        }
        if (!page.items.length || offset + page.items.length >= page.total) break;
        const unseen = page.items.filter(item => typeof item.id === "string" && !seen.has(item.id));
        if (!unseen.length) throw new Error("任务分页暂未更新，请重新读取上游要求。");
        for (const item of unseen) seen.add(item.id);
        offset += page.items.length;
      }
      if (current && !controller.signal.aborted) setState({ status: "missing" });
    }
    void read().catch(cause => { if (current && !isAbort(cause) && !controller.signal.aborted) setState({ status: "error", message: errorText(cause) }); });
    return () => { current = false; controller.abort(); };
  }, [projectId, retry]);
  return <section className={s.section} aria-label="项目上游要求" aria-busy={state.status === "loading"}>
    <div className={s.detailHead}><h2>上游需求与设计提示词</h2><Link href="/handoffs">返回我的任务 →</Link></div>
    {state.status === "loading" && <p className={s.subtle} role="status">正在核对当前项目的上游要求…</p>}
    {state.status === "missing" && <p className={s.subtle}>没有找到当前项目对应的授权任务。请返回我的任务，选择分派给你的设计任务。</p>}
    {state.status === "error" && <p className={s.error} role="alert">{state.message}</p>}
    {(state.status === "error" || state.status === "missing") && <button type="button" onClick={() => setRetry(value => value + 1)}>重新读取上游要求</button>}
    {state.status === "ready" && <SourceRequirements binding={state.binding} />}
  </section>;
}
