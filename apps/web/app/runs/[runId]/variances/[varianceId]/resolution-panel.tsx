"use client";

import { useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";
const DEMO_USER = process.env.NEXT_PUBLIC_DEMO_USER_ID || "00000000-0000-0000-0000-000000000001";

type Props = {
  varianceId: string;
  runId: string;
  currentStatus: string;
  requiresReviewerApproval: boolean;
};

export function ResolutionPanel({ varianceId, runId, currentStatus, requiresReviewerApproval }: Props) {
  const [note, setNote] = useState("Reviewed in Sprint 3 workflow");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);

  async function resolve(action: "matched" | "explained" | "expected_later" | "ignored") {
    setPending(true);
    setMessage("");
    try {
      const res = await fetch(`${API_BASE}/variances/${varianceId}/resolve`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-User-Id": DEMO_USER,
        },
        body: JSON.stringify({ action, note }),
      });
      const body = await res.json();
      if (!res.ok) {
        setMessage(`Failed: ${body?.detail || res.statusText}`);
      } else {
        setMessage(`Saved: ${body.status}`);
      }
    } catch (err) {
      setMessage(`Error: ${String(err)}`);
    } finally {
      setPending(false);
    }
  }

  async function approveIgnored() {
    setPending(true);
    setMessage("");
    try {
      const res = await fetch(`${API_BASE}/variances/${varianceId}/approve-ignored`, {
        method: "POST",
        headers: { "X-User-Id": DEMO_USER },
      });
      const body = await res.json();
      if (!res.ok) {
        setMessage(`Failed: ${body?.detail || res.statusText}`);
      } else {
        setMessage("Ignored variance approved by reviewer");
      }
    } catch (err) {
      setMessage(`Error: ${String(err)}`);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="card">
      <h2>Resolution Actions</h2>
      <p>
        <small>
          Run: {runId} | Current status: {currentStatus}
        </small>
      </p>
      <label htmlFor="resolution-note">Note</label>
      <textarea
        id="resolution-note"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={3}
        style={{ width: "100%", marginTop: "0.4rem", marginBottom: "0.8rem" }}
      />
      <div className="filters">
        <button disabled={pending} onClick={() => resolve("matched")}>
          Mark Matched
        </button>
        <button disabled={pending} onClick={() => resolve("explained")}>
          Mark Explained
        </button>
        <button disabled={pending} onClick={() => resolve("expected_later")}>
          Mark Expected Later
        </button>
        <button disabled={pending} onClick={() => resolve("ignored")}>
          Ignore (Reviewer Required)
        </button>
        {requiresReviewerApproval && (
          <button disabled={pending} onClick={approveIgnored}>
            Reviewer Approve Ignored
          </button>
        )}
      </div>
      {message && <p><small>{message}</small></p>}
    </section>
  );
}
