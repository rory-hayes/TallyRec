import "./globals.css";
import type { Metadata } from "next";
import { ReactNode } from "react";
import Link from "next/link";

import { getSession } from "../lib/session";

export const metadata: Metadata = {
  title: "Tally - Bank Tie-out",
  description: "Deterministic payroll reconciliation",
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const session = await getSession();
  return (
    <html lang="en">
      <body>
        <header className="topbar">
          <div className="topbar-inner">
            <nav className="topnav">
              <Link href="/">Home</Link>
              <Link href="/workspace">Workspace</Link>
              <Link href="/dashboard">Dashboard</Link>
              <Link href="/auth">Session</Link>
            </nav>
            <small>
              User: {session?.userId || "-"} | Firm: {session?.firmId || "-"}
            </small>
          </div>
        </header>
        {children}
      </body>
    </html>
  );
}
