/** Public same-origin path, fixed at build time together with next.config.basePath. */
export function appPath(path: string): string {
  const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
  if (!base || !path.startsWith("/") || path.startsWith("//") || path === base || path.startsWith(`${base}/`) || path.startsWith(`${base}?`) || path.startsWith(`${base}#`)) return path;
  return `${base}${path}`;
}

/** Normalize browser and Next pathname forms for route decisions, including the no-slash base root. */
export function appRelativePath(pathname: string): string {
  const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
  if (!base) return pathname;
  if (pathname === base || pathname === `${base}/`) return "/";
  return pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : pathname;
}
