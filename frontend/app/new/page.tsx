"use client";
import Link from "next/link";
import { TeamBoundary } from "@/components/team/TeamSession";
import { DirectRecovery, useDirectCreation } from "@/components/workspace/useDirectCreation";
import type { TeamUser } from "@/lib/team-api";
import s from "@/components/workspace/workspace.module.css";
export default function NewDesignPage() { return <TeamBoundary>{user => <NewEntry user={user} />}</TeamBoundary>; }
function NewEntry({ user }: { user: TeamUser }) {
  const creation = useDirectCreation(user);
  return <main className={s.home}><section className={s.creationHero}><h1>给想法，一张画布。</h1><p className={s.heroDescription}>直接进入空白画布，或回首页说说你的设计想法。</p>{user.role === "designer" && <button className={s.primary} disabled={creation.busy || !!creation.pending} onClick={() => void creation.blank()}>新建空白项目</button>}<p><Link href="/">返回创作首页 →</Link></p><DirectRecovery creation={creation} user={user} /></section></main>;
}
