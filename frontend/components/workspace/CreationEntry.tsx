"use client";
import { TeamBoundary } from "@/components/team/TeamSession";
import CreationHome from "./CreationHome";
export default function CreationEntry() { return <TeamBoundary>{user => <CreationHome user={user} />}</TeamBoundary>; }
