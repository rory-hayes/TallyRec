import Link from "next/link";

type Params = {
  params: Promise<{ runId: string }>;
  searchParams?: Promise<{ status?: string; category?: string; code?: string }>;
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

export default async function VarianceCenterPage({ params, searchParams }: Params) {
  const { runId } = await params;
  const resolvedSearchParams = (await searchParams) ?? {};
  const status = resolvedSearchParams.status || "open";
  const category = resolvedSearchParams.category || "";
  const code = resolvedSearchParams.code || "";

  const query = new URLSearchParams();
  query.set("status", status);
  if (category) query.set("category", category);
  if (code) query.set("code", code);

  const variances = (await fetchJson(`/runs/${runId}/variances?${query.toString()}`)) || [];

  return (
    <main>
      <h1>Variance Center</h1>
      <p>
        <small>Run ID: {runId}</small>
      </p>

      <div className="filters">
        <Link href={`/runs/${runId}/variances?status=open`}>Open</Link>
        <Link href={`/runs/${runId}/variances?status=resolved`}>Resolved</Link>
        <Link href={`/runs/${runId}/variances?status=ignored`}>Ignored</Link>
        <Link href={`/runs/${runId}`}>Back to Run</Link>
      </div>

      <section className="card">
        <table>
          <thead>
            <tr>
              <th>Code</th>
              <th>Category</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Amount</th>
              <th>Note</th>
              <th>Changed At</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {variances.map((item: any) => (
              <tr key={item.id}>
                <td>{item.code}</td>
                <td>{item.category}</td>
                <td>{item.severity}</td>
                <td>{item.status}</td>
                <td>{item.amount ?? "-"}</td>
                <td>{item.note ?? "-"}</td>
                <td>{item.changed_at ?? "-"}</td>
                <td>
                  <Link href={`/runs/${runId}/variances/${item.id}`}>View</Link>
                </td>
              </tr>
            ))}
            {variances.length === 0 && (
              <tr>
                <td colSpan={8}>No variances for selected filter.</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}
