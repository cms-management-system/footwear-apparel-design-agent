import Link from "next/link";
import { redirect } from "next/navigation";
import CreationEntry from "@/components/workspace/CreationEntry";
export default async function Page({ searchParams }: { searchParams: Promise<{ project?: string; handoff?: string }> }) {
  const query = await searchParams;
  if (query.project !== undefined) {
    const id = Number(query.project);
    if (!/^\d+$/.test(query.project) || !Number.isSafeInteger(id) || id < 1) return <main style={{ padding: 32 }}><h1>项目链接无效</h1><Link href="/projects">返回授权项目列表</Link></main>;
    redirect(`/projects/${id}${query.handoff ? `?handoff=${encodeURIComponent(query.handoff)}` : ""}`);
  }
  return <CreationEntry />;
}
