import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export type AppSession = {
  userId: string;
  firmId: string | null;
};

export const SESSION_USER_COOKIE = "tally_user_id";
export const SESSION_FIRM_COOKIE = "tally_firm_id";

export async function getSession(): Promise<AppSession | null> {
  const jar = await cookies();
  const userId = jar.get(SESSION_USER_COOKIE)?.value?.trim() || "";
  if (!userId) {
    return null;
  }
  const firmId = jar.get(SESSION_FIRM_COOKIE)?.value?.trim() || null;
  return { userId, firmId };
}

export async function requireSession(): Promise<AppSession> {
  const session = await getSession();
  if (!session) {
    redirect("/auth");
  }
  return session;
}
