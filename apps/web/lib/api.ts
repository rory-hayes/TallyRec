import type { AppSession } from "./session";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000/v1";

export async function fetchApi(path: string, session: AppSession, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers || {});
  headers.set("X-User-Id", session.userId);
  if (!headers.has("Content-Type") && init?.body) {
    headers.set("Content-Type", "application/json");
  }
  return fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    ...init,
    headers,
  });
}

export async function fetchApiJson<T>(path: string, session: AppSession, init?: RequestInit): Promise<T | null> {
  const res = await fetchApi(path, session, init);
  if (!res.ok) {
    return null;
  }
  return (await res.json()) as T;
}
