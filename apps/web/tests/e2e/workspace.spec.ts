import { expect, test } from "@playwright/test";

test("operator can create firm/client/run and enqueue bank reconcile from workspace", async ({ page }) => {
  const suffix = Date.now().toString().slice(-6);
  const userId = `00000000-0000-0000-0000-000000${suffix}`;
  const slug = `firm-${suffix}`;

  await page.goto("/auth");
  await page.getByTestId("auth-user-id").fill(userId);
  await page.getByTestId("auth-firm-id").fill("");
  await page.getByTestId("auth-next-path").fill("/workspace");
  await page.getByTestId("auth-save-btn").click();

  await expect(page.getByRole("heading", { name: "Operator Workspace" })).toBeVisible();

  await page.getByTestId("firm-name-input").fill(`Firm ${suffix}`);
  await page.getByTestId("firm-slug-input").fill(slug);
  await page.getByTestId("create-firm-btn").click();
  await expect(page.getByTestId("workspace-message")).toContainText("Firm created");

  await page.getByTestId("client-name-input").fill(`Client ${suffix}`);
  await page.getByTestId("client-ref-input").fill(`C-${suffix}`);
  await page.getByTestId("create-client-btn").click();
  await expect(page.getByTestId("workspace-message")).toContainText("Client created");

  await page.getByTestId("create-run-btn").click();
  await expect(page.getByTestId("workspace-message")).toContainText("Run created");

  await page.getByTestId("register-source-btn").click();
  await expect(page.getByTestId("workspace-message")).toContainText("Source file registered");

  await page.getByTestId("enqueue-bank-btn").click();
  await expect(page.getByTestId("workspace-message")).toContainText("Action complete");

  await page.getByRole("link", { name: "Open run page" }).click();
  await expect(page.getByRole("heading", { name: "Run Summary" })).toBeVisible();
});
