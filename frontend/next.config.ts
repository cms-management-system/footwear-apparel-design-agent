import type { NextConfig } from "next";

// Keep all Agent requests on the site's origin.
const BACKEND = process.env.BACKEND_BASE_URL ?? "http://127.0.0.1:8020";

const nextConfig: NextConfig = {
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*` }];
  },
};

export default nextConfig;
