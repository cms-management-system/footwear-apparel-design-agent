import Link from "next/link";
import ProjectEntry from "@/components/workspace/ProjectEntry";
export default async function ProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params; const id = Number(projectId);
  if (!/^\d+$/.test(projectId) || !Number.isSafeInteger(id) || id < 1) return <main style={{ padding: 32 }}><h1>项目链接无效</h1><p>请从授权项目列表重新打开。</p><Link href="/projects">返回设计项目</Link></main>;
  return <ProjectEntry projectId={id} />;
}
