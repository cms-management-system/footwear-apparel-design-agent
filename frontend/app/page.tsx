import DesignChat from "@/components/design/DesignChat";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export default async function Page() {
  const token = (await cookies()).get("design_session")?.value;
  if (!token) redirect("/login");
  const backend = process.env.BACKEND_BASE_URL || "http://127.0.0.1:8020";
  const response = await fetch(`${backend}/api/design-auth/me`, { headers: { Cookie: `design_session=${token}` }, cache: "no-store" });
  if (!response.ok) redirect("/login");
  const user = await response.json();
  if (user.role === "manager") redirect("/handoffs");
  return <DesignChat />;
}
