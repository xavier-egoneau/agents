import { expect, test, type Page, type Route } from "@playwright/test";

async function waitForHydration(page: Page) {
  await page.waitForFunction(() =>
    window.localStorage.getItem("amk.composer.preferences.v1") !== null);
}

async function installKernelMock(
  page: Page,
  override?: (route: Route, url: URL) => Promise<boolean>,
) {
  await page.route("**/api/kernel/**", async (route) => {
    const url = new URL(route.request().url());
    if (override && await override(route, url)) return;
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
    if (url.pathname.endsWith("/commands")) {
      return route.fulfill({ json: [] });
    }
    if (url.pathname.endsWith("/context-status")) {
      return route.fulfill({
        json: {
          context_window_tokens: null,
          estimated_history_tokens: 0,
          estimated_ratio: null,
        },
      });
    }
    if (url.pathname.endsWith("/plans/current")) {
      return route.fulfill({ json: null });
    }
    return route.fulfill({ json: {} });
  });
}

test.beforeEach(async ({ page }) => {
  await installKernelMock(page);
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

test("several messages continue in the same session", async ({ page }) => {
  const requests: Record<string, unknown>[] = [];
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (!url.pathname.endsWith("/runs") || route.request().method() !== "POST") return false;
    const body = route.request().postDataJSON() as Record<string, unknown>;
    requests.push(body);
    const index = requests.length;
    await route.fulfill({
      json: {
        session_id: body.session_id,
        run_id: `run-${index}`,
        agent_id: "main",
        status: "success",
        output: `Réponse ${index}`,
        errors: [],
        artifacts: [],
      },
    });
    return true;
  });
  await page.goto("/");
  await waitForHydration(page);

  const composer = page.getByLabel("Message au kernel");
  await composer.fill("Premier message");
  await expect(page.getByLabel("Envoyer")).toBeEnabled();
  await page.getByLabel("Envoyer").click();
  await expect(page.getByText("Réponse 1")).toBeVisible();
  await composer.fill("Deuxième message");
  await expect(page.getByLabel("Envoyer")).toBeEnabled();
  await page.getByLabel("Envoyer").click();
  await expect(page.getByText("Réponse 2")).toBeVisible();

  expect(requests).toHaveLength(2);
  expect(requests[0].session_id).toBeTruthy();
  expect(requests[1].session_id).toBe(requests[0].session_id);
});

test("an ASK remains actionable and resumes the suspended run", async ({ page }) => {
  const approval = {
    approval_id: "approval-1",
    session_id: "session-ask",
    run_id: "run-ask",
    tool_call_id: "call-1",
    tool_name: "browser_open",
    action_family: "network",
    justification: "Vérifier le rendu local",
    reason: "Local navigation requires approval.",
    risks: ["network"],
    path: "http://localhost:3000",
    created_at: "2026-07-24T16:00:00Z",
  };
  let resolved = false;
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/runs") && route.request().method() === "POST") {
      await route.fulfill({ json: {
        session_id: approval.session_id,
        run_id: approval.run_id,
        agent_id: "main",
        status: "approval_pending",
        output: "Approval required before the run can continue.",
        errors: [],
        artifacts: [],
      } });
      return true;
    }
    if (url.pathname.endsWith("/approvals")) {
      await route.fulfill({ json: resolved ? [] : [approval] });
      return true;
    }
    if (url.pathname.endsWith("/approvals/approval-1/resolve")) {
      resolved = true;
      await route.fulfill({ json: {
        session_id: approval.session_id, run_id: approval.run_id, agent_id: "main",
        status: "success", output: "Inspection terminée.", errors: [], artifacts: [],
      } });
      return true;
    }
    return false;
  });
  await page.goto("/");
  await waitForHydration(page);

  await page.getByLabel("Message au kernel").fill("Inspecte la page");
  await expect(page.getByLabel("Envoyer")).toBeEnabled();
  await page.getByLabel("Envoyer").click();
  await expect(page.getByText("Vérifier le rendu local")).toBeVisible();
  await page.getByRole("button", { name: "Autoriser" }).click();
  await expect(page.getByText("Inspection terminée.")).toBeVisible();
  await expect(page.getByText("Vérifier le rendu local")).not.toBeVisible();
});

test("composer preferences survive a page reload", async ({ page }) => {
  await page.goto("/");
  await waitForHydration(page);
  await page.getByLabel("Niveau de permission").selectOption("power");
  await page.getByLabel("Niveau de raisonnement").selectOption("high");
  await page.waitForFunction(() => {
    const raw = window.localStorage.getItem("amk.composer.preferences.v1");
    return raw?.includes('"securityMode":"power"') && raw.includes('"reasoning":"high"');
  });
  await page.reload();

  await expect(page.getByLabel("Niveau de permission")).toHaveValue("power");
  await expect(page.getByLabel("Niveau de raisonnement")).toHaveValue("high");
});

test("a session can be deleted from history", async ({ page }) => {
  let deleted = false;
  const session = {
    session_id: "11111111-1111-4111-8111-111111111111",
    agent_id: "main",
    prompt: "Session à supprimer",
    workspace: "/tmp/project",
    created_at: "2026-07-24T16:00:00Z",
    updated_at: "2026-07-24T16:01:00Z",
    status: "success",
    output: "Terminé",
    errors: [],
    event_count: 2,
    trigger: "user",
  };
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/sessions") && route.request().method() === "GET") {
      await route.fulfill({ json: deleted ? [] : [session] });
      return true;
    }
    if (
      url.pathname.endsWith(`/sessions/${session.session_id}`)
      && route.request().method() === "DELETE"
    ) {
      deleted = true;
      await route.fulfill({ json: { status: "deleted" } });
      return true;
    }
    return false;
  });
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("/");
  await waitForHydration(page);

  await expect(page.getByText("Session à supprimer")).toBeVisible();
  await page.getByLabel("Supprimer la session").click();
  await expect(page.getByText("Session à supprimer")).not.toBeVisible();
});
