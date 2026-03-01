import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { SESSION_FIRM_COOKIE, SESSION_USER_COOKIE } from "../../lib/session";

async function saveSession(formData: FormData) {
  "use server";
  const userId = String(formData.get("user_id") || "").trim();
  const firmId = String(formData.get("firm_id") || "").trim();
  const nextPath = String(formData.get("next_path") || "/dashboard").trim() || "/dashboard";
  if (!userId) {
    return;
  }
  const jar = await cookies();
  jar.set(SESSION_USER_COOKIE, userId, { httpOnly: true, sameSite: "lax", secure: true, path: "/" });
  if (firmId) {
    jar.set(SESSION_FIRM_COOKIE, firmId, { httpOnly: true, sameSite: "lax", secure: true, path: "/" });
  } else {
    jar.delete(SESSION_FIRM_COOKIE);
  }
  redirect(nextPath);
}

async function clearSession() {
  "use server";
  const jar = await cookies();
  jar.delete(SESSION_USER_COOKIE);
  jar.delete(SESSION_FIRM_COOKIE);
  redirect("/auth");
}

export default async function AuthPage() {
  const jar = await cookies();
  const userId = jar.get(SESSION_USER_COOKIE)?.value || "";
  const firmId = jar.get(SESSION_FIRM_COOKIE)?.value || "";

  return (
    <main>
      <h1>Session Setup</h1>
      <p>
        <small>Set the authenticated user and active firm context used by the UI API calls.</small>
      </p>

      <section className="card">
        <form action={saveSession} className="stack">
          <label htmlFor="user-id">User ID (UUID)</label>
          <input id="user-id" data-testid="auth-user-id" name="user_id" type="text" defaultValue={userId} required />

          <label htmlFor="firm-id">Active Firm ID (optional)</label>
          <input id="firm-id" data-testid="auth-firm-id" name="firm_id" type="text" defaultValue={firmId} />

          <label htmlFor="next-path">Redirect Path</label>
          <input id="next-path" data-testid="auth-next-path" name="next_path" type="text" defaultValue="/dashboard" />

          <button data-testid="auth-save-btn" type="submit">Save Session</button>
        </form>
      </section>

      <section className="card" style={{ marginTop: "1rem" }}>
        <form action={clearSession}>
          <button type="submit">Clear Session</button>
        </form>
      </section>
    </main>
  );
}
