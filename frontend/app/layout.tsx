import type { Metadata } from "next";
import PlatformBar from "@/components/design/PlatformBar";
import "./globals.css";

export const metadata: Metadata = {
  title: "鞋服设计 · 鞋服 AI 协作平台",
  description: "与设计助手共创鞋服款式，比较方向、生成和修改设计，选定成果。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <div className="wrap">
          <PlatformBar />
          {children}
        </div>
      </body>
    </html>
  );
}
