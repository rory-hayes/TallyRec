import Link from "next/link";

type SearchParams = {
  status?: string;
  due_before?: string;
  needs_attention?: string;
  client_id?: string;
  limit?: string;
  offset?: string;
};

type Params = {
  searchParams?: Promise<SearchParams>;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";
const DEMO_USER = process.env.NEXT_PUBLIC_DEMO_USER_ID || "00000000-0000-0000-0000-000000000001";
const DEMO_FIRM = process.env.NEXT_PUBLIC_DEMO_FIRM_ID || "";

async function fetchJson(path: string) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "X-User-Id": DEMO_USER },
    cache: "no-store",
  });
  if (!res.ok) {
    return null;
  }
  return res.json();
}

export default async function DashboardPage({ searchParams }: Params) {
  const resolvedSearchParams = (await searchParams) ?? {};

  if (!DEMO_FIRM) {
    return (
      <main>
        <h1>Bureau Dashboard</h1>
        <p>
          <small>Set NEXT_PUBLIC_DEMO_FIRM_ID to load dashboard data.</small>
        </p>
      </main>
    );
  }

  const query = new URLSearchParams();
  query.set("firm_id", DEMO_FIRM);
  if (resolvedSearchParams.status) query.set("status", resolvedSearchParams.status);
  if (resolvedSearchParams.due_before) query.set("due_before", resolvedSearchParams.due_before);
  if (resolvedSearchParams.needs_attention) query.set("needs_attention", resolvedSearchParams.needs_attention);
  if (resolvedSearchParams.client_id) query.set("client_id", resolvedSearchParams.client_id);
  if (resolvedSearchParams.limit) query.set("limit", resolvedSearchParams.limit);
  if (resolvedSearchParams.offset) query.set("offset", resolvedSearchParams.offset);

  const payload = await fetchJson(`/dashboard?${query.toString()}`);
  const rows = payload?.runs || [];
  const countsByStatus = payload?.counts_by_status || {};
  const dueCounts = payload?.counts_by_due_bucket || {};

  return (
    <main>
      <h1>Bureau Dashboard</h1>
      <p>
        <small>Firm: {DEMO_FIRM}</small>
      </p>

      <div className="filters">
        <Link href="/dashboard">All</Link>
        <Link href="/dashboard?needs_attention=true">Needs Attention</Link>
        <Link href="/dashboard?status=failed">Failed</Link>
        <Link href="/dashboard?status=approved">Approved</Link>
      </div>

      <section className="grid">
        <article className="card">
          <h2>By Status</h2>
          <pre>{JSON.stringify(countsByStatus, null, 2)}</pre>
        </article>
        <article className="card">
          <h2>By Due Bucket</h2>
          <pre>{JSON.stringify(dueCounts, null, 2)}</pre>
        </article>
      </section>

      <section className="card" style={{ marginTop: "1rem" }}>
        <h2>Runs</h2>
        <table>
          <thead>
            <tr>
              <th>Client</th>
              <th>Run</th>
              <th>Status</th>
              <th>Tie</th>
              <th>Blockers</th>
              <th>Review</th>
              <th>Must Close By</th>
              <th>SLA</th>
              <th>Import Health</th>
              <th>Latest Job</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row: any) => (
              <tr key={row.run_id}>
                <td>{row.client_name}</td>
                <td>
                  <Link href={`/runs/${row.run_id}`}>{row.run_id.slice(0, 8)}</Link>
                </td>
                <td>{row.run_status}</td>
                <td>{row.overall_tie_status}</td>
                <td>{row.open_blockers}</td>
                <td>{row.open_review}</td>
                <td>{row.must_close_by_date ?? "-"}</td>
                <td>{row.sla_reminder_state ?? "-"}</td>
                <td>{row.import_health_band ?? "-"}</td>
                <td>{row.latest_job_status ?? "-"}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={10}>No runs for filter.</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}
