import { expect, test, type Page } from "@playwright/test";
import { ADMIN_PASSWORD, REVIEWER_PASSWORD } from "../playwright.config";

async function login(page: Page, username: string, password: string) {
  await page.goto("/admin/");
  await page.getByLabel("Username").fill(username);
  await page.locator("input[autocomplete=current-password]").fill(password);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByTestId("current-user")).toHaveText(username);
}

test("reviewer approves a review item", async ({ page }) => {
  await login(page, "rita", REVIEWER_PASSWORD);
  const nav = page.getByLabel("Main navigation");
  await expect(nav.getByRole("link", { name: "Users" })).toHaveCount(0);

  const rows = page.locator('[data-testid^="review-row-"]');
  await expect(rows.first()).toBeVisible();
  const before = await rows.count();
  await rows.first().click();
  await expect(page.getByRole("table", { name: "Score breakdown" })).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(rows).toHaveCount(before - 1);
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible();
});

test("admin creates an ontology draft and publishes it", async ({ page }) => {
  await login(page, "alice", ADMIN_PASSWORD);
  await page.getByLabel("Main navigation").getByRole("link", { name: "Ontology" }).click();
  await expect(page).toHaveURL(/#\/ontology$/);
  await expect(page.getByTestId("ontology-graph")).toBeVisible();
  await expect(page.locator(".react-flow__node").first()).toBeVisible();

  await page.getByRole("button", { name: "Create draft" }).click();
  const publish = page.getByRole("button", { name: /^Publish v\d+$/ });
  await expect(publish).toBeVisible();
  const label = (await publish.textContent()) ?? "";
  const version = label.replace("Publish v", "");
  await publish.click();
  await expect(publish).toHaveCount(0);
  const row = page.getByRole("table", { name: "Ontology versions" }).getByRole("row", { name: new RegExp(`v${version}`) });
  await expect(row).toContainText("published");
});
