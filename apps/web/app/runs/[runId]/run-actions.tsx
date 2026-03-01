"use client";

import { useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";

type Props = {
  runId: string;
  userId: string;
  locked: boolean;
};

export function RunActions({ runId, userId, locked }: Props) {
  const [note, setNote] = useState("Operator action");
  const [asOfDate, setAsOfDate] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);

  async function call(path: string, body?: Record<string, unknown>) {
    setPending(true);
    setMessage("");
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-User-Id": userId,
        },
        body: JSON.stringify(body || {}),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
      } else {
        setMessage(`Success: ${path}`);
      }
    } catch (error) {
      setMessage(`Error: ${String(error)}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="card" style={{ marginTop: "1rem" }}>
      <h2>Run Actions</h2>
      <p><small>Locked: {String(locked)}</small></p>

      <label htmlFor="run-note">Note</label>
      <input id="run-note" type="text" value={note} onChange={(e) => setNote(e.target.value)} />

      <label htmlFor="run-as-of">As Of Date (optional)</label>
      <input id="run-as-of" type="date" value={asOfDate} onChange={(e) => setAsOfDate(e.target.value)} />

      <div className="filters">
        <button disabled={pending} onClick={() => call(`/runs/${runId}/reconcile/bank`, asOfDate ? { as_of_date: asOfDate } : {})}>
          Enqueue Bank Reconcile
        </button>
        <button disabled={pending} onClick={() => call(`/runs/${runId}/reconcile/gl`, {})}>
          Enqueue GL Reconcile
        </button>
        <button disabled={pending} onClick={() => call(`/runs/${runId}/ready-for-review`, { note })}>
          Ready For Review
        </button>
        <button disabled={pending} onClick={() => call(`/runs/${runId}/approve`, { note })}>
          Approve Run
        </button>
        <button disabled={pending} onClick={() => call(`/runs/${runId}/unlock`, { reason: note })}>
          Unlock Run
        </button>
        <button disabled={pending} onClick={() => call(`/runs/${runId}/export-pack`, {})}>
          Enqueue Export Pack
        </button>
      </div>
      {message && <p><small>{message}</small></p>}
    </section>
  );
}
