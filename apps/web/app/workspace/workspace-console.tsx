"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";

type Firm = {
  id: string;
  name: string;
  slug: string;
  role?: string;
};

type Client = {
  id: string;
  name: string;
  external_ref?: string | null;
};

type Run = {
  id: string;
  client_id: string;
  client_name: string;
  status: string;
  period_start: string;
  period_end: string;
};

type Props = {
  userId: string;
  initialFirmId: string | null;
  initialFirms: Firm[];
  initialClients: Client[];
  initialRuns: Run[];
  queueStats: {
    counts: Record<string, number>;
    oldest_queued_at: string | null;
  } | null;
};

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function monthStartIso(): string {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10);
}

export function WorkspaceConsole({
  userId,
  initialFirmId,
  initialFirms,
  initialClients,
  initialRuns,
  queueStats,
}: Props) {
  const [firms, setFirms] = useState<Firm[]>(initialFirms);
  const [activeFirmId, setActiveFirmId] = useState(initialFirmId || initialFirms[0]?.id || "");
  const [clients, setClients] = useState<Client[]>(initialClients);
  const [runs, setRuns] = useState<Run[]>(initialRuns);
  const [selectedClientId, setSelectedClientId] = useState(initialClients[0]?.id || "");
  const [selectedRunId, setSelectedRunId] = useState(initialRuns[0]?.id || "");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);

  const [firmName, setFirmName] = useState("");
  const [firmSlug, setFirmSlug] = useState("");
  const [clientName, setClientName] = useState("");
  const [clientExternalRef, setClientExternalRef] = useState("");
  const [periodStart, setPeriodStart] = useState(monthStartIso());
  const [periodEnd, setPeriodEnd] = useState(todayIso());
  const [fileKind, setFileKind] = useState("bank");
  const [filename, setFilename] = useState("bank.csv");
  const [fileUri, setFileUri] = useState("s3://bucket/bank.csv");
  const [checksum, setChecksum] = useState("a".repeat(64));
  const [byteSize, setByteSize] = useState("123");
  const [mappingTemplateId, setMappingTemplateId] = useState("");
  const [observedHeaders, setObservedHeaders] = useState("");
  const [actionNote, setActionNote] = useState("Operator action");
  const [asOfDate, setAsOfDate] = useState("");

  const sortedRuns = useMemo(() => runs, [runs]);

  async function api(path: string, init?: RequestInit) {
    const headers = new Headers(init?.headers || {});
    headers.set("X-User-Id", userId);
    if (init?.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(`${API_BASE}${path}`, { ...init, headers });
  }

  async function refreshFirmContext(firmId: string) {
    if (!firmId) {
      setClients([]);
      setRuns([]);
      return;
    }
    const [clientsRes, runsRes, firmsRes] = await Promise.all([
      api(`/firms/${firmId}/clients`),
      api(`/runs?firm_id=${firmId}`),
      api("/firms"),
    ]);
    if (firmsRes.ok) {
      setFirms(await firmsRes.json());
    }
    if (clientsRes.ok) {
      const payload = await clientsRes.json();
      setClients(payload.clients || []);
      if (!selectedClientId && payload.clients?.[0]?.id) {
        setSelectedClientId(payload.clients[0].id);
      }
    }
    if (runsRes.ok) {
      const payload = await runsRes.json();
      setRuns(payload.runs || []);
      if (!selectedRunId && payload.runs?.[0]?.id) {
        setSelectedRunId(payload.runs[0].id);
      }
    }
  }

  async function withPending(fn: () => Promise<void>) {
    setPending(true);
    setMessage("");
    try {
      await fn();
    } catch (error) {
      setMessage(`Error: ${String(error)}`);
    } finally {
      setPending(false);
    }
  }

  async function createFirm() {
    await withPending(async () => {
      const res = await api("/firms", {
        method: "POST",
        body: JSON.stringify({ name: firmName, slug: firmSlug }),
      });
      const payload = await res.json();
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Firm created: ${payload.id}`);
      setActiveFirmId(payload.id);
      setFirmName("");
      setFirmSlug("");
      await refreshFirmContext(payload.id);
    });
  }

  async function createClient() {
    if (!activeFirmId) return;
    await withPending(async () => {
      const res = await api("/clients", {
        method: "POST",
        body: JSON.stringify({
          firm_id: activeFirmId,
          name: clientName,
          external_ref: clientExternalRef || null,
        }),
      });
      const payload = await res.json();
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Client created: ${payload.id}`);
      setSelectedClientId(payload.id);
      setClientName("");
      setClientExternalRef("");
      await refreshFirmContext(activeFirmId);
    });
  }

  async function createRun() {
    if (!activeFirmId || !selectedClientId) return;
    await withPending(async () => {
      const res = await api("/runs", {
        method: "POST",
        body: JSON.stringify({
          firm_id: activeFirmId,
          client_id: selectedClientId,
          period_start: periodStart,
          period_end: periodEnd,
        }),
      });
      const payload = await res.json();
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Run created: ${payload.id}`);
      setSelectedRunId(payload.id);
      await refreshFirmContext(activeFirmId);
    });
  }

  async function registerSourceFile() {
    if (!selectedRunId) return;
    await withPending(async () => {
      const body: Record<string, unknown> = {
        file_kind: fileKind,
        filename,
        uri: fileUri,
        checksum_sha256: checksum,
        byte_size: Number(byteSize) || 0,
      };
      if (mappingTemplateId) body.mapping_template_id = mappingTemplateId;
      if (observedHeaders.trim()) {
        body.observed_headers = observedHeaders.split(",").map((item) => item.trim()).filter(Boolean);
      }
      const res = await api(`/runs/${selectedRunId}/source-files`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      const payload = await res.json();
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Source file registered: ${payload.id}`);
    });
  }

  async function runAction(path: string, body: Record<string, unknown>) {
    if (!selectedRunId) return;
    await withPending(async () => {
      const res = await api(path.replace("{runId}", selectedRunId), {
        method: "POST",
        body: JSON.stringify(body),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Action complete: ${path}`);
      if (activeFirmId) {
        await refreshFirmContext(activeFirmId);
      }
    });
  }

  return (
    <div className="stack">
      <section className="card">
        <h2>Session</h2>
        <p>
          <small>User: {userId}</small>
        </p>
        <p>
          <small>Queue: {JSON.stringify(queueStats?.counts || {})}</small>
        </p>
        <p>
          <small>Oldest queued job: {queueStats?.oldest_queued_at || "-"}</small>
        </p>
      </section>

      <section className="card">
        <h2>Firm Context</h2>
        <label htmlFor="active-firm-id">Active firm</label>
        <div className="inline-row">
          <select
            id="active-firm-id"
            value={activeFirmId}
            onChange={async (e) => {
              const next = e.target.value;
              setActiveFirmId(next);
              await refreshFirmContext(next);
            }}
          >
            <option value="">Select firm</option>
            {firms.map((firm) => (
              <option key={firm.id} value={firm.id}>
                {firm.name} ({firm.role || "-"})
              </option>
            ))}
          </select>
          <button disabled={pending || !activeFirmId} onClick={() => refreshFirmContext(activeFirmId)}>
            Refresh
          </button>
        </div>

        <h3>Create Firm</h3>
        <div className="inline-row">
          <input data-testid="firm-name-input" placeholder="Firm name" value={firmName} onChange={(e) => setFirmName(e.target.value)} />
          <input data-testid="firm-slug-input" placeholder="slug" value={firmSlug} onChange={(e) => setFirmSlug(e.target.value)} />
          <button data-testid="create-firm-btn" disabled={pending || !firmName || !firmSlug} onClick={createFirm}>
            Create Firm
          </button>
        </div>
      </section>

      <section className="card">
        <h2>Client + Run Setup</h2>
        <div className="inline-row">
          <input data-testid="client-name-input" placeholder="Client name" value={clientName} onChange={(e) => setClientName(e.target.value)} />
          <input
            data-testid="client-ref-input"
            placeholder="External ref"
            value={clientExternalRef}
            onChange={(e) => setClientExternalRef(e.target.value)}
          />
          <button data-testid="create-client-btn" disabled={pending || !activeFirmId || !clientName} onClick={createClient}>
            Create Client
          </button>
        </div>

        <div className="inline-row" style={{ marginTop: "0.75rem" }}>
          <select data-testid="client-select" value={selectedClientId} onChange={(e) => setSelectedClientId(e.target.value)}>
            <option value="">Select client</option>
            {clients.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </select>
          <input data-testid="period-start-input" type="date" value={periodStart} onChange={(e) => setPeriodStart(e.target.value)} />
          <input data-testid="period-end-input" type="date" value={periodEnd} onChange={(e) => setPeriodEnd(e.target.value)} />
          <button data-testid="create-run-btn" disabled={pending || !selectedClientId} onClick={createRun}>
            Create Run
          </button>
        </div>
      </section>

      <section className="card">
        <h2>Register Source File</h2>
        <div className="inline-row">
          <select data-testid="run-select-source" value={selectedRunId} onChange={(e) => setSelectedRunId(e.target.value)}>
            <option value="">Select run</option>
            {sortedRuns.map((run) => (
              <option key={run.id} value={run.id}>
                {run.client_name} | {run.period_start} {"->"} {run.period_end}
              </option>
            ))}
          </select>
          <select value={fileKind} onChange={(e) => setFileKind(e.target.value)}>
            <option value="bank">bank</option>
            <option value="payroll">payroll</option>
            <option value="gl">gl</option>
          </select>
          <input value={filename} onChange={(e) => setFilename(e.target.value)} />
          <input value={fileUri} onChange={(e) => setFileUri(e.target.value)} />
        </div>
        <div className="inline-row" style={{ marginTop: "0.75rem" }}>
          <input value={checksum} onChange={(e) => setChecksum(e.target.value)} />
          <input value={byteSize} onChange={(e) => setByteSize(e.target.value)} />
          <input
            placeholder="mapping template id (optional)"
            value={mappingTemplateId}
            onChange={(e) => setMappingTemplateId(e.target.value)}
          />
          <input
            placeholder="observed headers csv (optional)"
            value={observedHeaders}
            onChange={(e) => setObservedHeaders(e.target.value)}
          />
          <button data-testid="register-source-btn" disabled={pending || !selectedRunId} onClick={registerSourceFile}>
            Register
          </button>
        </div>
      </section>

      <section className="card">
        <h2>Run Lifecycle Actions</h2>
        <div className="inline-row">
          <select data-testid="run-select-action" value={selectedRunId} onChange={(e) => setSelectedRunId(e.target.value)}>
            <option value="">Select run</option>
            {sortedRuns.map((run) => (
              <option key={run.id} value={run.id}>
                {run.id.slice(0, 8)} | {run.client_name} | {run.status}
              </option>
            ))}
          </select>
          <input value={actionNote} onChange={(e) => setActionNote(e.target.value)} placeholder="note / reason" />
          <input type="date" value={asOfDate} onChange={(e) => setAsOfDate(e.target.value)} />
        </div>
        <div className="filters">
          <button
            data-testid="enqueue-bank-btn"
            disabled={pending || !selectedRunId}
            onClick={() =>
              runAction(`/runs/{runId}/reconcile/bank`, asOfDate ? { as_of_date: asOfDate } : { payload: {} })
            }
          >
            Enqueue Bank
          </button>
          <button data-testid="enqueue-gl-btn" disabled={pending || !selectedRunId} onClick={() => runAction(`/runs/{runId}/reconcile/gl`, { payload: {} })}>
            Enqueue GL
          </button>
          <button disabled={pending || !selectedRunId} onClick={() => runAction(`/runs/{runId}/ready-for-review`, { note: actionNote })}>
            Ready for Review
          </button>
          <button disabled={pending || !selectedRunId} onClick={() => runAction(`/runs/{runId}/approve`, { note: actionNote })}>
            Approve
          </button>
          <button disabled={pending || !selectedRunId} onClick={() => runAction(`/runs/{runId}/unlock`, { reason: actionNote })}>
            Unlock
          </button>
          <button disabled={pending || !selectedRunId} onClick={() => runAction(`/runs/{runId}/export-pack`, {})}>
            Export Pack
          </button>
        </div>
        {selectedRunId && (
          <p>
            <Link href={`/runs/${selectedRunId}`}>Open run page</Link> |{" "}
            <Link href={`/runs/${selectedRunId}/imports`}>Open import mapping</Link>
          </p>
        )}
      </section>

      <section className="card">
        <h2>Current Runs</h2>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Client</th>
              <th>Status</th>
              <th>Period</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sortedRuns.map((run) => (
              <tr key={run.id}>
                <td>{run.id.slice(0, 8)}</td>
                <td>{run.client_name}</td>
                <td>{run.status}</td>
                <td>
                  {run.period_start} {"->"} {run.period_end}
                </td>
                <td>
                  <Link data-testid={`run-open-${run.id}`} href={`/runs/${run.id}`}>Open</Link>
                </td>
              </tr>
            ))}
            {sortedRuns.length === 0 && (
              <tr>
                <td colSpan={5}>No runs found for the selected firm.</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {message && (
        <section className="card" data-testid="workspace-message">
          <small>{message}</small>
        </section>
      )}
    </div>
  );
}
