import Link from "next/link";

import { fetchApiJson } from "../../../../lib/api";
import { requireSession } from "../../../../lib/session";
import { ImportsPanel } from "./imports-panel";

type Params = {
  params: Promise<{ runId: string }>;
};

export default async function RunImportsPage({ params }: Params) {
  const session = await requireSession();
  const { runId } = await params;
  const summary = await fetchApiJson<any>(`/runs/${runId}/summary`, session);

  if (!summary) {
    return (
      <main>
        <h1>Run Imports</h1>
        <p>Run not found.</p>
      </main>
    );
  }

  const files = (await fetchApiJson<any[]>(`/runs/${runId}/source-files`, session)) || [];
  const templates = (await fetchApiJson<any[]>(`/clients/${summary.client_id}/mapping-templates`, session)) || [];

  return (
    <main>
      <h1>Run Import Mapping</h1>
      <p>
        <small>Run: {runId}</small>
      </p>
      <p>
        <Link href={`/runs/${runId}`}>Back to Run Summary</Link>
      </p>
      <ImportsPanel
        userId={session.userId}
        runId={runId}
        clientId={summary.client_id}
        initialFiles={files}
        initialTemplates={templates}
      />
    </main>
  );
}
