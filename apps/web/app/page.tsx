import Link from "next/link";

import { getSession } from "../lib/session";

export default async function HomePage() {
  const session = await getSession();
  return (
    <main>
      <h1>Tally Sprint 4 UI</h1>
      <p><small>Deterministic payroll reconciliation operator console.</small></p>
      <section className="card">
        <p>Open operator tools at <Link href="/workspace">/workspace</Link>.</p>
        <p>Open bureau dashboard at <Link href="/dashboard">/dashboard</Link>.</p>
        <p>Open a run at <code>/runs/&lt;runId&gt;</code>.</p>
      </section>
      <section className="card" style={{ marginTop: "1rem" }}>
        <h2>Current Session</h2>
        <p><small>User ID: {session?.userId || "not set"}</small></p>
        <p><small>Firm ID: {session?.firmId || "not set"}</small></p>
        {!session && (
          <p>
            <Link href="/auth">Set session context</Link>
          </p>
        )}
      </section>
    </main>
  );
}
