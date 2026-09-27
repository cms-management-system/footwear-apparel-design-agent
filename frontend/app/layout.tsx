import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "款式工场 · 鞋服智能设计",
  description: "与设计助手共创鞋服款式，比较方向、生成和修改设计，选定成果。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <div className="wrap">
          <header className="bar">
            <b>款式工场</b>
            <span>AI Design Studio</span>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
