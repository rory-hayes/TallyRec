import Link from "next/link";
import { ResolutionPanel } from "./resolution-panel";

type Params = {
  params: Promise<{ runId: string; varianceId: string }>;
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

export default async function VarianceDetailPage({ params }: Params) {
  const { runId, varianceId } = await params;
  const variance = await fetchJson(`/runs/${runId}/variances/${varianceId}`);

  if (!variance) {
    return (
      <main>
        <h1>Variance Detail</h1>
        <p>Variance not found.</p>
        <Link href={`/runs/${runId}/variances`}>Back</Link>
      </main>
    );
  }

  return (
    <main>
      <h1>Variance Detail</h1>
      <p>
        <small>
          {variance.code} | {variance.category} | {variance.severity}
        </small>
      </p>

      <section className="card">
        <p><strong>Status:</strong> {variance.status}</p>
        <p><strong>Message:</strong> {variance.message}</p>
        <p><strong>Amount:</strong> {variance.amount ?? "-"}</p>
        <p><strong>Date:</strong> {variance.event_date ?? "-"}</p>
        <p><strong>Account:</strong> {variance.account_ref ?? "-"}</p>
        <p><strong>Note:</strong> {variance.note ?? "-"}</p>
        <p><strong>Changed By:</strong> {variance.changed_by ?? "-"}</p>
        <p><strong>Changed At:</strong> {variance.changed_at ?? "-"}</p>
      </section>

      <ResolutionPanel
        runId={runId}
        varianceId={varianceId}
        currentStatus={variance.status}
        requiresReviewerApproval={Boolean(variance.ignored_needs_reviewer_approval)}
      />

      <section className="card" style={{ marginTop: "1rem" }}>
        <h2>Resolution History</h2>
        <table>
          <thead>
            <tr>
              <th>Action</th>
              <th>Note</th>
              <th>Actor</th>
              <th>Timestamp</th>
            </tr>
          </thead>
          <tbody>
            {(variance.events || []).map((event: any) => (
              <tr key={event.id}>
                <td>{event.action}</td>
                <td>{event.note ?? "-"}</td>
                <td>{event.actor_user_id ?? "-"}</td>
                <td>{event.created_at}</td>
              </tr>
            ))}
            {(variance.events || []).length === 0 && (
              <tr>
                <td colSpan={4}>No resolution events yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <p style={{ marginTop: "1rem" }}>
        <Link href={`/runs/${runId}/variances`}>Back to Variance Center</Link>
      </p>
    </main>
  );
}
