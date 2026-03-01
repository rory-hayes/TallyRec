import Link from "next/link";

type Params = {
  params: Promise<{ runId: string }>;
  searchParams?: Promise<{ severity?: string; status?: string; code?: string }>;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";
const DEMO_USER = process.env.NEXT_PUBLIC_DEMO_USER_ID || "00000000-0000-0000-0000-000000000001";

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

function statusClass(status?: string | null) {
  if (status === "Tied") return "badge tied";
  if (status === "Needs review") return "badge needs-review";
  return "badge not-tied";
}

export default async function RunPage({ params, searchParams }: Params) {
  const { runId } = await params;
  const resolvedSearchParams = (await searchParams) ?? {};
  const filterStatus = resolvedSearchParams.status || "open";
  const summary = await fetchJson(`/runs/${runId}/summary`);
  const tieout = await fetchJson(`/runs/${runId}/bank-tieout`);
  const variances = (await fetchJson(`/runs/${runId}/variances?category=bank&status=${filterStatus}`)) || [];
  const matchGroups = (await fetchJson(`/runs/${runId}/match-groups`)) || [];

  return (
    <main>
      <h1>Run Summary</h1>
      <p>
        <small>Run ID: {runId}</small>
      </p>
      <p>
        <span className={statusClass(summary?.tieout?.status)}>{summary?.tieout?.status || "Not tied"}</span>
      </p>
      <p>
        <small>
          Overall tie status: {summary?.overall_tie_status ?? "-"} | GL status: {summary?.gl_tieout?.status ?? "-"} | Locked:{" "}
          {String(summary?.locked ?? false)}
        </small>
      </p>
      <p>
        <small>
          Payday: {summary?.payday_date ?? "-"} | Must close by: {summary?.must_close_by_date ?? "-"} | SLA:{" "}
          {summary?.sla_reminder_state ?? "-"} | Import health: {summary?.import_health?.band ?? "-"}
        </small>
      </p>

      <section className="grid">
        <article className="card">
          <h2>Expected Net Pay</h2>
          <strong>{summary?.tieout?.expected_net_pay ?? "-"}</strong>
        </article>
        <article className="card">
          <h2>Matched Bank Total</h2>
          <strong>{summary?.tieout?.matched_bank_total ?? "-"}</strong>
        </article>
        <article className="card">
          <h2>Delta</h2>
          <strong>{summary?.tieout?.delta ?? "-"}</strong>
        </article>
      </section>

      <section className="card" style={{ marginTop: "1rem" }}>
        <h2>Bank tie-out</h2>
        <p>
          <small>
            Tolerance: {tieout?.policy?.amount_tolerance ?? "-"} | Window: {tieout?.policy?.date_window_days ?? "-"} days |
            Max group: {tieout?.policy?.max_group_size ?? "-"} | Require allowlist: {String(tieout?.policy?.require_allowed_account)}
          </small>
        </p>
        <p>
          <small>
            Timing snapshot: {JSON.stringify(tieout?.timing ?? summary?.tieout?.policy_snapshot?.timing ?? {})}
          </small>
        </p>
        <table>
          <thead>
            <tr>
              <th>Kind</th>
              <th>Expected</th>
              <th>Bank</th>
              <th>Delta</th>
              <th>Members</th>
            </tr>
          </thead>
          <tbody>
            {matchGroups.map((item: any) => (
              <tr key={item.id}>
                <td>{item.group_kind}</td>
                <td>{item.expected_total}</td>
                <td>{item.bank_total}</td>
                <td>{item.delta}</td>
                <td>{item.members_count}</td>
              </tr>
            ))}
            {matchGroups.length === 0 && (
              <tr>
                <td colSpan={5}>No match groups yet</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="card" style={{ marginTop: "1rem" }}>
        <h2>Variances</h2>
        <div className="filters">
          <Link href={`/runs/${runId}?status=open`}>Open</Link>
          <Link href={`/runs/${runId}?status=resolved`}>Resolved</Link>
          <Link href={`/runs/${runId}`}>Reset</Link>
          <Link href={`/runs/${runId}/variances`}>Open Variance Center</Link>
          <Link href="/dashboard">Dashboard</Link>
        </div>
        <table>
          <thead>
            <tr>
              <th>Code</th>
              <th>Severity</th>
              <th>Message</th>
              <th>Amount</th>
              <th>Date</th>
              <th>Account</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {variances.map((item: any) => (
              <tr key={item.id}>
                <td>{item.code}</td>
                <td>{item.severity}</td>
                <td>{item.message}</td>
                <td>{item.amount ?? "-"}</td>
                <td>{item.event_date ?? "-"}</td>
                <td>{item.account_ref ?? "-"}</td>
                <td>{item.status}</td>
              </tr>
            ))}
            {variances.length === 0 && (
              <tr>
                <td colSpan={7}>No variances for filter</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}
