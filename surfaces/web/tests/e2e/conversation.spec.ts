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

test("a completed cron is flagged unread without rendering its result in management", async ({ page }) => {
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/crons/runs")) {
      await route.fulfill({
        json: [{
          id: "cronrun-1",
          cron_job_id: "cron-1",
          scheduled_for: "2026-07-31T09:00:00Z",
          claimed_at: "2026-07-31T09:00:01Z",
          started_at: "2026-07-31T09:00:01Z",
          completed_at: "2026-07-31T09:00:02Z",
          session_id: "session-cron-1",
          notification_session_id: "33333333-3333-4333-8333-333333333333",
          run_id: "run-cron-1",
          execution_status: "success",
          task_status: "success",
          delivery_status: "unread",
          output_preview: "Bonjour depuis la routine",
          error: null,
        }],
      });
      return true;
    }
    if (url.pathname.endsWith("/crons")) {
      await route.fulfill({
        json: [{
          id: "cron-1",
          name: "Bonjour",
          schedule: "*/3 * * * *",
          prompt: "Dis bonjour",
          workspace: "/tmp/project",
          agent_id: "main",
          skills: [],
          security_mode: "limited",
          provider_id: "test",
          model: "test-model",
          reasoning: "medium",
          enabled: true,
          auto_resume: true,
          session_id: "session-cron-1",
          notification_session_id: "33333333-3333-4333-8333-333333333333",
          next_run_at: "2026-07-31T09:03:00Z",
          last_run_at: "2026-07-31T09:00:00Z",
          last_status: "success",
          last_error: null,
          last_retryable: false,
          in_flight: false,
          blocked: false,
        }],
      });
      return true;
    }
    return false;
  });

  await page.goto("/");
  await expect(page.getByText("1 nouveau résultat")).toBeVisible();
  await page.getByRole("button", { name: /Automatisations/ }).click();
  await expect(page.getByText("Bonjour", { exact: true })).toBeVisible();
  await expect(page.getByText("Nouveaux résultats")).not.toBeVisible();
  await expect(page.getByText("Bonjour depuis la routine")).not.toBeVisible();
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

test("routine pre-validation groups one scope into one consent and acknowledges it immediately", async ({ page }) => {
  const runId = "22222222-2222-4222-8222-222222222222";
  const sessionId = "11111111-1111-4111-8111-111111111111";
  const approvals = [0, 1].map((index) => ({
    approval_id: `00000000-0000-4000-8000-00000000000${index}`,
    session_id: sessionId,
    run_id: runId,
    tool_call_id: `call-${index}`,
    tool_name: "web_search",
    action_family: "network",
    justification: `Recherche ${index + 1}`,
    reason: "Network approval required",
    risks: ["network"],
    path: null,
    created_at: `2026-08-02T10:00:0${index}Z`,
  }));
  let resolved = false;
  let resolveRequests = 0;
  let releaseResolution: (() => void) | undefined;
  const resolutionGate = new Promise<void>((resolve) => {
    releaseResolution = resolve;
  });
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/crons/cron-1/approval-status")) {
      await route.fulfill({ json: resolved
        ? { approved_scopes: [{ tool_name: "web_search", action_family: "network", path: null }], pending_count: 0, pending_run_id: null }
        : { approved_scopes: [], pending_count: 2, pending_run_id: runId } });
      return true;
    }
    if (url.pathname.endsWith("/approvals/resolve-batch")) {
      resolveRequests += 1;
      expect(route.request().postDataJSON()).toEqual({
        approval_ids: approvals.map((item) => item.approval_id),
        approved: true,
      });
      await resolutionGate;
      resolved = true;
      await route.fulfill({ json: {
        session_id: sessionId, run_id: runId, agent_id: "main",
        status: "success", output: "Test terminé", errors: [], artifacts: [],
      } });
      return true;
    }
    if (url.pathname.endsWith("/approvals")) {
      await route.fulfill({ json: resolved ? [] : approvals });
      return true;
    }
    if (url.pathname.endsWith("/crons")) {
      await route.fulfill({ json: [{
        id: "cron-1", name: "Veille", schedule: "*/3 * * * *", prompt: "Cherche",
        workspace: "/tmp/project", agent_id: "main", skills: [], security_mode: "limited",
        provider_id: "test", model: "test-model", reasoning: "medium", enabled: false,
        auto_resume: true, session_id: sessionId,
        notification_session_id: "33333333-3333-4333-8333-333333333333",
        next_run_at: null, last_run_at: null, last_status: "approval_pending",
        last_error: null, last_retryable: false, in_flight: false, blocked: false,
      }] });
      return true;
    }
    return false;
  });

  await page.goto("/");
  await waitForHydration(page);
  await page.getByRole("button", { name: /Automatisations/ }).click();
  await page.getByText("Veille", { exact: true }).click();
  await expect(page.getByText("1 autorisation pour 2 actions attend ta décision.")).toBeVisible();
  await expect(page.locator(".cron-test-panel li")).toHaveCount(1);
  await expect(page.locator(".cron-test-panel li")).toContainText("2 actions prévues");
  await expect(page.getByText("Recherche 2", { exact: true })).not.toBeVisible();

  await page.getByRole("button", { name: "Autoriser cette routine" }).click();
  await expect.poll(() => resolveRequests).toBe(1);
  await expect(page.getByRole("button", { name: "Autorisation en cours…" })).toBeDisabled();
  await expect(page.locator(".cron-test-panel")).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("Recherche 1")).toBeVisible();
  await expect(page.getByText(/Validation de 1 autorisation.*2 action/)).toBeVisible();

  releaseResolution?.();
  await expect(page.getByText(/Confirmé · 1 autorisation.*2 action/)).toBeVisible();
  await expect(page.getByText("Prévalidation active")).toBeVisible();
  await expect(page.locator(".cron-test-panel")).toHaveAttribute("aria-busy", "false");
  expect(resolveRequests).toBe(1);
});

test("a management error keeps its own row above the routine name", async ({ page }) => {
  const sessionId = "11111111-1111-4111-8111-111111111111";
  const cron = {
    id: "cron-layout", name: "Routine avec erreur", schedule: "*/3 * * * *", prompt: "Cherche",
    workspace: "/tmp/project", agent_id: "main", skills: [], security_mode: "limited",
    provider_id: "test", model: "test-model", reasoning: "medium", enabled: false,
    auto_resume: true, session_id: sessionId,
    notification_session_id: "33333333-3333-4333-8333-333333333333",
    next_run_at: null, last_run_at: null, last_status: null,
    last_error: null, last_retryable: false, in_flight: false, blocked: false,
  };
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/crons/cron-layout/approval-status")) {
      await route.fulfill({
        json: { approved_scopes: [], pending_count: 0, pending_run_id: null },
      });
      return true;
    }
    if (
      url.pathname.endsWith("/crons/cron-layout")
      && route.request().method() === "PUT"
    ) {
      await route.fulfill({
        status: 409,
        json: { detail: "Impossible d'enregistrer cette routine pour le moment." },
      });
      return true;
    }
    if (url.pathname.endsWith("/crons")) {
      await route.fulfill({ json: [cron] });
      return true;
    }
    return false;
  });

  await page.goto("/");
  await waitForHydration(page);
  await page.getByRole("button", { name: /Automatisations/ }).click();
  await page.getByText("Routine avec erreur", { exact: true }).click();
  await expect(page.getByLabel("Nom")).toBeVisible();
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();

  const alert = page.locator(".management-error");
  const editor = page.locator(".cron-editor");
  const nameField = editor.locator("label.field-wide").first();
  await expect(alert).toHaveRole("alert");
  await expect(alert).toContainText("Impossible d'enregistrer cette routine");
  await editor.evaluate((element) => { element.scrollTop = 0; });
  await expect(nameField).toBeVisible();
  const alertBox = await alert.boundingBox();
  const nameFieldBox = await nameField.boundingBox();
  expect(alertBox).not.toBeNull();
  expect(nameFieldBox).not.toBeNull();
  expect(alertBox!.y + alertBox!.height).toBeLessThanOrEqual(nameFieldBox!.y);
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
