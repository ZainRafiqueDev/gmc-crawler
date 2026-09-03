import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import NavLink from "@/components/NavLink";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "GMC Compliance Checker",
  description: "Google Merchant Center policy compliance auditor",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <div className="bg-aurora" aria-hidden="true" />
        <header className="sticky top-0 z-20 border-b border-surface-border/80 bg-background/70 backdrop-blur-md">
          <nav className="max-w-4xl mx-auto px-4 py-3 flex items-center gap-6">
            <Link href="/" className="font-semibold tracking-tight text-lg gradient-text">
              GMC Compliance Checker
            </Link>
            <NavLink href="/">Run Audit</NavLink>
            <NavLink href="/monitor">Monitored Stores</NavLink>
          </nav>
        </header>
        <main className="flex-1 max-w-4xl mx-auto w-full px-4 py-10">{children}</main>
        <footer className="text-center text-xs text-slate-400 py-6">
          Automated GMC policy checks - always confirm critical findings before acting.
        </footer>
      </body>
    </html>
  );
}
