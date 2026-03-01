import Link from "next/link";
import { RunActions } from "./run-actions";
import { fetchApiJson } from "../../../lib/api";
import { requireSession } from "../../../lib/session";

type Params = {
  params: Promise<{ runId: string }>;
  searchParams?: Promise<{ severity?: string; status?: string; code?: string }>;
};

function statusClass(status?: string | null) {
  if (status === "Tied") return "badge tied";
  if (status === "Needs review") return "badge needs-review";
  return "badge not-tied";
}

export default async function RunPage({ params, searchParams }: Params) {
  const session = await requireSession();
  const { runId } = await params;
  const resolvedSearchParams = (await searchParams) ?? {};
  const filterStatus = resolvedSearchParams.status || "open";
  const summary = await fetchApiJson<any>(`/runs/${runId}/summary`, session);
  const tieout = await fetchApiJson<any>(`/runs/${runId}/bank-tieout`, session);
  const variances = (await fetchApiJson<any[]>(`/runs/${runId}/variances?category=bank&status=${filterStatus}`, session)) || [];
  const matchGroups = (await fetchApiJson<any[]>(`/runs/${runId}/match-groups`, session)) || [];

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
          <Link href={`/runs/${runId}/imports`}>Import Mapping</Link>
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

      <RunActions runId={runId} userId={session.userId} locked={Boolean(summary?.locked)} />
    </main>
  );
}
