import SelectedDesignPage from "@/components/design/SelectedDesignPage";

export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ versionId: string }>;
  searchParams: Promise<{ project?: string }>;
}) {
  const { versionId } = await params;
  const { project } = await searchParams;
  return <SelectedDesignPage versionId={versionId} projectId={Number(project)} />;
}
