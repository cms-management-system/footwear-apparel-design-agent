"use client";
import { TeamBoundary } from "@/components/team/TeamSession";
import ProjectWorkspace from "./ProjectWorkspace";
export default function ProjectEntry({ projectId }: { projectId: number }) { return <TeamBoundary>{user => <ProjectWorkspace key={`${projectId}:${user.username}:${user.auth_context_id}`} projectId={projectId} user={user} />}</TeamBoundary>; }
