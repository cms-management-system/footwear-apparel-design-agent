"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { appRelativePath } from "@/lib/app-path";

function SeriesMark() {
  return <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m12 2 9 5v10l-9 5-9-5V7l9-5Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" /><path d="m3.5 7.5 8.5 5 8.5-5M12 12.5v9M7.5 5l9 5v5" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" /></svg>;
}

function RailIcon({ type }: { type: "tasks" | "design" | "settings" }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {type === "tasks" ? <><rect x="4" y="5" width="16" height="16" rx="3" /><path d="M9 5V3h6v2M8 10h8M8 14h5M8 18h3" /></> : type === "design" ? <><path d="m15 4 5 5M4 20l5-1 12-12a2 2 0 0 0-5-5L4 14l-1 7 6-2" /><path d="m4 14 5 5" /></> : <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" fill="var(--c-surface)" /><circle cx="15" cy="17" r="3" fill="var(--c-surface)" /></>}
  </svg>;
}

const siblingLinks = [
  { label: "穿搭", href: process.env.NEXT_PUBLIC_LINK_OUTFIT },
  { label: "产品", href: process.env.NEXT_PUBLIC_LINK_PRODUCT },
].filter((link): link is { label: string; href: string } => Boolean(link.href));

export default function PlatformBar() {
  const pathname = appRelativePath(usePathname());
  const minimal = pathname === "/login" || pathname === "/register";
  const canvas = /^\/projects\/[^/]+$/.test(pathname);
  const creative = canvas || pathname === "/";
  const section = pathname.startsWith("/handoffs") ? "tasks" : pathname.startsWith("/settings") ? "settings" : "design";
  const navigation = [
    { key: "tasks" as const, href: "/handoffs", label: "任务" },
    { key: "design" as const, href: "/", label: "创作" },
    { key: "settings" as const, href: "/settings", label: "设置" },
  ];

  return <>
    {!minimal && <aside className="platformRail" aria-label="鞋服设计导航">
      <Link href="/" className="platformRailBrand" aria-label="鞋服设计创作首页"><SeriesMark /></Link>
      <nav aria-label="设计工作台">{navigation.map(item => <Link key={item.key} href={item.href} className="platformRailLink" aria-current={section === item.key ? "page" : undefined}><RailIcon type={item.key} /><span>{item.label}</span></Link>)}</nav>
      <span className="platformRailCaption" aria-hidden="true">鞋服设计</span>
    </aside>}
    <header className="platformBar" data-minimal={minimal} hidden={creative}>
      <Link href={minimal ? "/login" : "/handoffs"} className="platformBrand">
        {minimal && <span className="platformProductMark"><SeriesMark /></span>}
        <b>鞋服设计</b><i aria-hidden="true" /><span>从明确需求，到可审查方案</span>
      </Link>
      {!minimal && siblingLinks.length > 0 ? <nav className="platformNav" aria-label="相关产品">{siblingLinks.map(link => <a key={link.label} href={link.href}>{link.label}</a>)}</nav> : <span className="platformContext">{minimal ? "团队工作区" : "设计工作台"}</span>}
    </header>
  </>;
}
