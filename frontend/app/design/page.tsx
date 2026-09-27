import { redirect } from "next/navigation";

// Keep saved links usable while all new designs share the homepage entry.
export default async function DesignPage({ searchParams }: { searchParams: Promise<{ project?: string }> }) {
  const { project } = await searchParams;
  redirect(project && /^\d+$/.test(project) ? `/?project=${project}` : "/");
}
