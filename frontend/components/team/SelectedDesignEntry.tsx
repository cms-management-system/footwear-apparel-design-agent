"use client";
import SelectedDesignPage from "@/components/design/SelectedDesignPage";
import { TeamBoundary } from "./TeamSession";
export default function SelectedDesignEntry({ versionId, projectId }: { versionId: string; projectId: number }) {
  return <TeamBoundary>{user => <SelectedDesignPage versionId={versionId} projectId={projectId} readOnly={user.role === "manager"} />}</TeamBoundary>;
}
