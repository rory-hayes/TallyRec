import Link from "next/link";

import { fetchApiJson } from "../../lib/api";
import { requireSession } from "../../lib/session";

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

export default async function DashboardPage({ searchParams }: Params) {
  const session = await requireSession();
  const resolvedSearchParams = (await searchParams) ?? {};
  const sessionPayload = await fetchApiJson<any>("/session", session);
  const fallbackFirmId = sessionPayload?.default_firm_id || sessionPayload?.memberships?.[0]?.firm_id || "";
  const firmId = session.firmId || fallbackFirmId;

  const query = new URLSearchParams();
  if (firmId) {
    query.set("firm_id", firmId);
  }
  if (resolvedSearchParams.status) query.set("status", resolvedSearchParams.status);
  if (resolvedSearchParams.due_before) query.set("due_before", resolvedSearchParams.due_before);
  if (resolvedSearchParams.needs_attention) query.set("needs_attention", resolvedSearchParams.needs_attention);
  if (resolvedSearchParams.client_id) query.set("client_id", resolvedSearchParams.client_id);
  if (resolvedSearchParams.limit) query.set("limit", resolvedSearchParams.limit);
  if (resolvedSearchParams.offset) query.set("offset", resolvedSearchParams.offset);

  const payload = firmId ? await fetchApiJson<any>(`/dashboard?${query.toString()}`, session) : null;
  const rows = payload?.runs || [];
  const countsByStatus = payload?.counts_by_status || {};
  const dueCounts = payload?.counts_by_due_bucket || {};

  return (
    <main>
      <h1>Bureau Dashboard</h1>
      <p>
        <small>Firm: {firmId || "not set"}</small>
      </p>
      {!firmId && <p><small>Set an active firm on /auth or create one on /workspace.</small></p>}

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
