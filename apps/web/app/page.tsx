import Link from "next/link";

export default function HomePage() {
  return (
    <main>
      <h1>Tally Sprint 4 UI</h1>
      <p>
        Open a run summary at <code>/runs/&lt;runId&gt;</code>.
      </p>
      <p>
        Open the bureau dashboard at <Link href="/dashboard">/dashboard</Link>.
      </p>
    </main>
  );
}
