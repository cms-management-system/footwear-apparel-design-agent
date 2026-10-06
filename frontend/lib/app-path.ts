/** Public same-origin path, fixed at build time together with next.config.basePath. */
export function appPath(path: string): string {
  const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";
  if (!base || !path.startsWith("/") || path.startsWith("//") || path === base || path.startsWith(`${base}/`)) return path;
  return `${base}${path}`;
}
