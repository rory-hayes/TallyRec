import { fetchApiJson } from "../../lib/api";
import { requireSession } from "../../lib/session";
import { WorkspaceConsole } from "./workspace-console";

export default async function WorkspacePage() {
  const session = await requireSession();
  const sessionPayload = await fetchApiJson<any>("/session", session);
  const firms = (await fetchApiJson<any[]>("/firms", session)) || [];
  const activeFirmId = session.firmId || sessionPayload?.default_firm_id || firms[0]?.id || null;
  const clientsPayload = activeFirmId
    ? await fetchApiJson<any>(`/firms/${activeFirmId}/clients`, session)
    : { clients: [] };
  const runsPayload = activeFirmId ? await fetchApiJson<any>(`/runs?firm_id=${activeFirmId}`, session) : { runs: [] };
  const queueStats = await fetchApiJson<any>("/ops/queue-stats", session);

  return (
    <main>
      <h1>Operator Workspace</h1>
      <p>
        <small>Create firm/client/run, register files, enqueue reconcile jobs, and drive approval/export workflows.</small>
      </p>
      <WorkspaceConsole
        userId={session.userId}
        initialFirmId={activeFirmId}
        initialFirms={firms}
        initialClients={clientsPayload?.clients || []}
        initialRuns={runsPayload?.runs || []}
        queueStats={queueStats}
      />
    </main>
  );
}
