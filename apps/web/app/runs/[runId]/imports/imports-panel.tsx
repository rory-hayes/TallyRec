"use client";

import { useMemo, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";

type SourceFile = {
  id: string;
  file_kind: string;
  filename: string;
  uri: string;
  mapping_template_id: string | null;
  observed_headers: string[] | null;
  import_validation_status: string;
  created_at: string;
};

type MappingTemplate = {
  id: string;
  name: string;
  file_kind: string;
  expected_headers: string[] | null;
  expected_header_hash: string | null;
};

type Props = {
  userId: string;
  runId: string;
  clientId: string;
  initialFiles: SourceFile[];
  initialTemplates: MappingTemplate[];
};

export function ImportsPanel({ userId, runId, clientId, initialFiles, initialTemplates }: Props) {
  const [files, setFiles] = useState<SourceFile[]>(initialFiles);
  const [templates, setTemplates] = useState<MappingTemplate[]>(initialTemplates);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");

  const [templateName, setTemplateName] = useState("Bank CSV v1");
  const [templateFileKind, setTemplateFileKind] = useState("bank");
  const [templateHeaders, setTemplateHeaders] = useState("date,amount,account_ref,description");
  const [templateMappingJson, setTemplateMappingJson] = useState(
    JSON.stringify({ date: "date", amount: "amount", account_ref: "account_ref", description: "description" }),
  );

  const [rowTemplateIds, setRowTemplateIds] = useState<Record<string, string>>({});
  const [rowObservedHeaders, setRowObservedHeaders] = useState<Record<string, string>>({});

  const templateOptionsByKind = useMemo(() => {
    const out: Record<string, MappingTemplate[]> = {};
    for (const template of templates) {
      if (!out[template.file_kind]) {
        out[template.file_kind] = [];
      }
      out[template.file_kind].push(template);
    }
    return out;
  }, [templates]);

  async function api(path: string, init?: RequestInit) {
    const headers = new Headers(init?.headers || {});
    headers.set("X-User-Id", userId);
    if (init?.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(`${API_BASE}${path}`, { ...init, headers });
  }

  async function refresh() {
    const [filesRes, templatesRes] = await Promise.all([
      api(`/runs/${runId}/source-files`),
      api(`/clients/${clientId}/mapping-templates`),
    ]);
    if (filesRes.ok) {
      setFiles(await filesRes.json());
    }
    if (templatesRes.ok) {
      setTemplates(await templatesRes.json());
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

  async function upsertTemplate() {
    await withPending(async () => {
      let parsedMapping: Record<string, string>;
      try {
        parsedMapping = JSON.parse(templateMappingJson);
      } catch {
        setMessage("Invalid mapping JSON");
        return;
      }
      const headers = templateHeaders
        .split(",")
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean);
      const res = await api(`/clients/${clientId}/mapping-templates`, {
        method: "POST",
        body: JSON.stringify({
          name: templateName,
          file_kind: templateFileKind,
          expected_headers: headers,
          mapping: parsedMapping,
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Template saved: ${payload.id}`);
      await refresh();
    });
  }

  async function remapFile(sourceFileId: string, fileKind: string) {
    const mappingTemplateId = rowTemplateIds[sourceFileId];
    if (!mappingTemplateId) {
      setMessage("Select a mapping template id");
      return;
    }
    await withPending(async () => {
      const observedHeadersRaw = rowObservedHeaders[sourceFileId] || "";
      const observedHeaders = observedHeadersRaw
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean);
      const res = await api(`/source-files/${sourceFileId}/remap`, {
        method: "POST",
        body: JSON.stringify({
          mapping_template_id: mappingTemplateId,
          observed_headers: observedHeaders.length > 0 ? observedHeaders : undefined,
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMessage(`Failed: ${payload?.detail || res.statusText}`);
        return;
      }
      setMessage(`Source file remapped (${fileKind}): ${payload.import_validation_status}`);
      await refresh();
    });
  }

  return (
    <div className="stack">
      <section className="card">
        <h2>Mapping Templates</h2>
        <div className="inline-row">
          <input value={templateName} onChange={(e) => setTemplateName(e.target.value)} placeholder="Template name" />
          <select value={templateFileKind} onChange={(e) => setTemplateFileKind(e.target.value)}>
            <option value="bank">bank</option>
            <option value="payroll">payroll</option>
            <option value="gl">gl</option>
          </select>
          <input
            value={templateHeaders}
            onChange={(e) => setTemplateHeaders(e.target.value)}
            placeholder="expected headers csv"
          />
          <button disabled={pending} onClick={upsertTemplate}>
            Save Template
          </button>
        </div>
        <textarea
          value={templateMappingJson}
          onChange={(e) => setTemplateMappingJson(e.target.value)}
          rows={4}
          style={{ width: "100%", marginTop: "0.75rem" }}
        />
      </section>

      <section className="card">
        <h2>Source Files + Drift Remediation</h2>
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th>Kind</th>
              <th>Validation</th>
              <th>Observed Headers</th>
              <th>Template</th>
              <th>Remap</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.id}>
                <td>{file.filename}</td>
                <td>{file.file_kind}</td>
                <td>{file.import_validation_status}</td>
                <td>{(file.observed_headers || []).join(", ") || "-"}</td>
                <td>
                  <select
                    value={rowTemplateIds[file.id] || file.mapping_template_id || ""}
                    onChange={(e) => setRowTemplateIds((prev) => ({ ...prev, [file.id]: e.target.value }))}
                  >
                    <option value="">Select template</option>
                    {(templateOptionsByKind[file.file_kind] || []).map((template) => (
                      <option key={template.id} value={template.id}>
                        {template.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    placeholder="observed headers csv (optional)"
                    value={rowObservedHeaders[file.id] || ""}
                    onChange={(e) => setRowObservedHeaders((prev) => ({ ...prev, [file.id]: e.target.value }))}
                  />
                  <button disabled={pending} onClick={() => remapFile(file.id, file.file_kind)} style={{ marginTop: "0.4rem" }}>
                    Remap
                  </button>
                </td>
              </tr>
            ))}
            {files.length === 0 && (
              <tr>
                <td colSpan={6}>No source files registered for run.</td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {message && (
        <section className="card">
          <small>{message}</small>
        </section>
      )}
    </div>
  );
}
