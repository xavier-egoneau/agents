import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/api/kernel/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/health")) {
      return route.fulfill({ json: { status: "ok" } });
    }
    if (url.pathname.endsWith("/workspaces/current")) {
      return route.fulfill({
        json: { path: "/tmp/project", name: "project", readable: true, writable: true },
      });
    }
    if (url.pathname.endsWith("/catalog")) {
      return route.fulfill({
        json: {
          default_provider: "test",
          agents: [{ id: "main", description: "Main", provider: "test" }],
          skills: [],
          providers: [{ id: "test", kind: "test", models: ["test-model"] }],
          tools: [],
        },
      });
    }
    if (url.pathname.endsWith("/sessions")) {
      return route.fulfill({ json: [] });
    }
    if (url.pathname.endsWith("/approvals")) {
      return route.fulfill({ json: [] });
    }
    if (url.pathname.endsWith("/crons")) {
      return route.fulfill({ json: [] });
    }
    return route.fulfill({ json: {} });
  });
});

test("composer exposes durable permission, model and reasoning controls", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Agentic kernel")).toBeVisible();
  await expect(page.getByText("Projet actif")).toBeVisible();
  await expect(page.getByText("Conversation active")).toBeVisible();
  await expect(page.locator("textarea")).toBeVisible();
  await expect(page.locator("select").filter({ has: page.locator('option[value="limited"]') }).first())
    .toHaveValue("limited");
});

test("management cards and history remain directly accessible", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Agent actif")).toBeVisible();
  await expect(page.getByText("Skills", { exact: true })).toBeVisible();
  await expect(page.getByText("Providers", { exact: true })).toBeVisible();
  await expect(page.getByText("Automatisations", { exact: true })).toBeVisible();
  await expect(page.getByText("Historique", { exact: true })).toBeVisible();
});
