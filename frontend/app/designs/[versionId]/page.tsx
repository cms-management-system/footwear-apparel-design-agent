import SelectedDesignEntry from "@/components/team/SelectedDesignEntry";
export default async function Page({ params, searchParams }: { params: Promise<{ versionId: string }>; searchParams: Promise<{ project?: string }> }) {
  const { versionId } = await params;
  const { project } = await searchParams;
  return <SelectedDesignEntry versionId={versionId} projectId={Number(project)} />;
}
