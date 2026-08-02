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

test("a new routine previews and saves a workflow only after explicit acceptance", async ({ page }) => {
  const createdBodies: Record<string, unknown>[] = [];
  let proposalRequests = 0;
  let releaseFirstProposal: (() => void) | undefined;
  const firstProposalGate = new Promise<void>((resolve) => {
    releaseFirstProposal = resolve;
  });
  const workflow = {
    schema: "amk.workflow/v1",
    id: "routine-veille-v1",
    title: "Veille quotidienne",
    status: "ready",
    execution: { mode: "agent_guided", deviation: "stop_and_report", timezone: "Europe/Paris" },
    missing_dependencies: [],
    steps: [
      {
        id: "news",
        title: "Recherche du jour",
        description: "Rechercher les actualités technologiques importantes publiées ce matin.",
        kind: "tool",
        tool: "web_search",
        needs: [],
        args: { query: "actualités technologiques importantes de ce matin", limit: 5 },
      },
      {
        id: "summary",
        title: "Synthèse sourcée",
        description: "Résumer les éléments importants et conserver les liens vers les sources.",
        kind: "synthesize",
        needs: ["news"],
      },
    ],
  };
  const proposal = { workflow, basis_hash: "basis-veille", warnings: [] };
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/catalog")) {
      await route.fulfill({ json: {
        default_provider: "test",
        agents: [{ id: "main", description: "Main", provider: "test" }],
        skills: [
          { name: "daily-review", description: "Cadre une synthèse quotidienne" },
          { name: "workflow-creator", description: "Conçoit les workflows" },
        ],
        providers: [{ id: "test", kind: "test", models: ["test-model"] }],
        tools: [{ name: "web_search", description: "Recherche web", module: "web", risks: ["network"] }],
      } });
      return true;
    }
    if (url.pathname.endsWith("/sessions")) {
      await route.fulfill({ json: [{
        session_id: "33333333-3333-4333-8333-333333333333",
        agent_id: "main", prompt: "Routines", workspace: null,
        created_at: "2026-08-02T08:00:00Z", updated_at: "2026-08-02T08:00:00Z",
        status: "success", output: null, errors: [], event_count: 0, trigger: "routine_inbox",
      }] });
      return true;
    }
    if (url.pathname.endsWith("/crons/workflow-proposals")) {
      proposalRequests += 1;
      const body = route.request().postDataJSON() as Record<string, unknown>;
      expect(body.prompt).toBe(proposalRequests === 1
        ? "Fais la veille du matin. Résume les choses importantes."
        : "Fais une veille tech du matin avec des sources.");
      expect(body.skills).toEqual(["daily-review"]);
      if (proposalRequests === 1) await firstProposalGate;
      await route.fulfill({ json: proposal });
      return true;
    }
    if (url.pathname.endsWith("/crons") && route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      createdBodies.push(body);
      await route.fulfill({ json: {
        id: "cron-workflow", ...body, workflow, workflow_revision: 1,
        workflow_basis_hash: "basis-veille", workflow_updated_at: "2026-08-02T10:00:00Z",
        session_id: "11111111-1111-4111-8111-111111111111",
        next_run_at: null, last_run_at: null, last_status: null, last_error: null,
        last_retryable: false, in_flight: false, blocked: false,
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons")) {
      await route.fulfill({ json: [] });
      return true;
    }
    return false;
  });

  await page.goto("/");
  await waitForHydration(page);
  await page.getByRole("button", { name: /Automatisations/ }).click();
  await page.getByRole("button", { name: "Ajouter" }).click();

  await expect(page.getByRole("group", { name: "Skills de la routine" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /workflow-creator/ })).not.toBeVisible();
  await page.getByRole("checkbox", { name: /daily-review/ }).check();
  const routinePrompt = page.getByLabel("Demande exécutée");
  await routinePrompt.fill("Fais la veille du matin. Résume les choses importantes.");
  await expect(page.getByText("Mode libre — sans workflow", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Préparer un workflow guidé" }).click();

  await expect(page.getByRole("button", { name: "Proposition en cours…" })).toBeDisabled();
  await expect(page.locator(".routine-workflow-panel")).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("Analyse du prompt, des skills et des outils disponibles…")).toHaveRole("status");
  await routinePrompt.fill("Fais une veille tech du matin avec des sources.");
  await expect(page.getByText(/génération a été annulée.*base de la routine a changé/i)).toBeVisible();
  releaseFirstProposal?.();

  await expect(page.getByText("Proposition non enregistrée")).not.toBeVisible();
  await page.getByRole("button", { name: "Préparer un workflow guidé" }).click();
  await expect(page.getByText("Proposition non enregistrée")).toBeVisible();
  const workflowPanel = page.locator(".routine-workflow-panel");
  await expect(workflowPanel.getByText("Déroulé proposé", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText("2 étapes", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText("Recherche du jour", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText(
    "Rechercher les actualités technologiques importantes publiées ce matin.",
    { exact: true },
  )).toBeVisible();
  await expect(workflowPanel.getByText("Recherche web", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText("web_search", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText(
    "actualités technologiques importantes de ce matin",
    { exact: true },
  )).toBeVisible();
  await expect(workflowPanel.getByText("Synthèse sourcée", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText(
    "Résumer les éléments importants et conserver les liens vers les sources.",
    { exact: true },
  )).toBeVisible();
  await expect(page.getByText(/Faible.*écart non prévu/)).toBeVisible();
  expect(createdBodies).toHaveLength(0);

  await expect(workflowPanel.getByRole("button", { name: "Continuer sans workflow" })).toBeVisible();
  await expect(workflowPanel.getByRole("button", { name: "Accepter ce workflow" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Créer avec ce workflow" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Ignorer" })).toHaveCount(0);
  await workflowPanel.getByRole("button", { name: "Accepter ce workflow" }).click();

  await expect(page.getByText("Routine et workflow enregistrés. Le prompt et les skills sont conservés.")).toBeVisible();
  expect(createdBodies).toHaveLength(1);
  expect(createdBodies[0].skills).toEqual(["daily-review"]);
  expect(createdBodies[0].accepted_workflow).toEqual(proposal);
});

test("a routine can be created with skills and no workflow", async ({ page }) => {
  let createdBody: Record<string, unknown> | null = null;
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/catalog")) {
      await route.fulfill({ json: {
        default_provider: "test",
        agents: [{ id: "main", description: "Main", provider: "test" }],
        skills: [{ name: "daily-review", description: "Cadre la synthèse" }],
        providers: [{ id: "test", kind: "test", models: ["test-model"] }],
        tools: [],
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons") && route.request().method() === "POST") {
      createdBody = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ json: {
        id: "cron-free", ...createdBody,
        workflow: null, workflow_revision: null, workflow_basis_hash: null, workflow_updated_at: null,
        session_id: "11111111-1111-4111-8111-111111111111",
        notification_session_id: "33333333-3333-4333-8333-333333333333",
        next_run_at: null, last_run_at: null, last_status: null, last_error: null,
        last_retryable: false, in_flight: false, blocked: false,
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons")) {
      await route.fulfill({ json: [] });
      return true;
    }
    return false;
  });

  await page.goto("/");
  await waitForHydration(page);
  await page.getByRole("button", { name: /Automatisations/ }).click();
  await page.getByRole("button", { name: "Ajouter" }).click();
  await page.getByRole("checkbox", { name: /daily-review/ }).check();
  await page.getByLabel("Demande exécutée").fill("Résume les éléments importants.");
  await page.getByRole("button", { name: "Créer en mode libre" }).click();

  await expect(page.getByText("Routine enregistrée sans workflow · exécution par prompt et skills.")).toBeVisible();
  expect(createdBody).not.toBeNull();
  expect(createdBody!.skills).toEqual(["daily-review"]);
  expect(createdBody).not.toHaveProperty("accepted_workflow");
});

test("a ready workflow proposal replaces a pending free-mode prevalidation only after acceptance", async ({ page }) => {
  const sessionId = "41111111-1111-4111-8111-111111111111";
  const runId = "42222222-2222-4222-8222-222222222222";
  const putBodies: Record<string, unknown>[] = [];
  const workflow = {
    schema: "amk.workflow/v1",
    id: "routine-veille-guidee-v1",
    title: "Veille guidée",
    status: "ready",
    execution: { mode: "agent_guided", deviation: "stop_and_report", timezone: "Europe/Paris" },
    missing_dependencies: [],
    steps: [
      {
        id: "search-news",
        title: "Rechercher les actualités du matin",
        description: "Identifier les informations récentes qui méritent d’être signalées.",
        kind: "tool",
        tool: "web_search",
        needs: [],
        args: { query: "actualités importantes ce matin", limit: 5 },
      },
      {
        id: "summarize",
        title: "Préparer la synthèse finale",
        description: "Hiérarchiser les résultats et citer les sources retenues.",
        kind: "synthesize",
        needs: ["search-news"],
      },
    ],
  };
  const proposal = { workflow, basis_hash: "basis-free-routine", warnings: [] };
  const cron = {
    id: "cron-free-awaiting-test",
    name: "Veille libre à optimiser",
    schedule: "0 9 * * *",
    prompt: "Résume les actualités importantes du matin.",
    workspace: "/tmp/project",
    agent_id: "main",
    skills: ["daily-review"],
    security_mode: "limited",
    provider_id: "test",
    model: "test-model",
    reasoning: "medium",
    enabled: false,
    auto_resume: true,
    session_id: sessionId,
    notification_session_id: "43333333-3333-4333-8333-333333333333",
    next_run_at: null,
    last_run_at: null,
    last_status: "approval_pending",
    last_error: null,
    last_retryable: false,
    in_flight: false,
    blocked: false,
    workflow: null,
    workflow_revision: 0,
    workflow_basis_hash: null,
    workflow_updated_at: null,
  };
  const approval = {
    approval_id: "40000000-0000-4000-8000-000000000001",
    session_id: sessionId,
    run_id: runId,
    tool_call_id: "call-search-news",
    tool_name: "web_search",
    action_family: "network",
    justification: "Rechercher les actualités importantes du matin",
    reason: "Network approval required",
    risks: ["network"],
    path: null,
    created_at: "2026-08-02T10:00:00Z",
  };

  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/catalog")) {
      await route.fulfill({ json: {
        default_provider: "test",
        agents: [{ id: "main", description: "Main", provider: "test" }],
        skills: [
          { name: "daily-review", description: "Cadre une synthèse quotidienne" },
          { name: "workflow-creator", description: "Conçoit les workflows" },
        ],
        providers: [{ id: "test", kind: "test", models: ["test-model"] }],
        tools: [{ name: "web_search", description: "Recherche web", module: "web", risks: ["network"] }],
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons/cron-free-awaiting-test/approval-status")) {
      await route.fulfill({ json: {
        approved_scopes: [], pending_count: 1, pending_run_id: runId,
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons/workflow-proposals") && route.request().method() === "POST") {
      await route.fulfill({ json: proposal });
      return true;
    }
    if (
      url.pathname.endsWith("/crons/cron-free-awaiting-test")
      && route.request().method() === "PUT"
    ) {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      putBodies.push(body);
      await route.fulfill({ json: {
        ...cron,
        ...body,
        workflow,
        workflow_revision: 1,
        workflow_basis_hash: proposal.basis_hash,
        workflow_updated_at: "2026-08-02T10:05:00Z",
        last_status: null,
      } });
      return true;
    }
    if (url.pathname.endsWith("/approvals")) {
      await route.fulfill({ json: [approval] });
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
  await page.getByText("Veille libre à optimiser", { exact: true }).click();

  const testPanel = page.locator(".cron-test-panel");
  await expect(testPanel).toBeVisible();
  await expect(testPanel.getByText("1 autorisation pour 1 action attend ta décision.")).toBeVisible();

  const workflowPanel = page.locator(".routine-workflow-panel");
  await workflowPanel.getByRole("button", { name: "Préparer un workflow guidé" }).click();

  await expect(workflowPanel.getByText("Proposition non enregistrée", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText("Rechercher les actualités du matin", { exact: true })).toBeVisible();
  await expect(workflowPanel.getByText("Préparer la synthèse finale", { exact: true })).toBeVisible();
  await expect(page.locator(".cron-test-panel")).toHaveCount(0);
  await expect(workflowPanel.getByRole("button", { name: "Continuer sans workflow" })).toBeEnabled();
  const acceptWorkflow = workflowPanel.getByRole("button", { name: "Accepter ce workflow" });
  await expect(acceptWorkflow).toBeEnabled();
  await expect(page.getByRole("button", { name: "Créer avec ce workflow" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Ignorer" })).toHaveCount(0);

  await acceptWorkflow.click();

  await expect.poll(() => putBodies.length).toBe(1);
  expect(putBodies[0].accepted_workflow).toEqual(proposal);
  await expect(page.getByText("Workflow actif · v1", { exact: true })).toBeVisible();
});

test("a blocked free-mode routine can still prepare a workflow and explains a legacy 404", async ({ page }) => {
  const sessionId = "11111111-1111-4111-8111-111111111111";
  const runId = "22222222-2222-4222-8222-222222222222";
  let proposalRequests = 0;
  let workflowReadRequests = 0;
  const approval = {
    approval_id: "00000000-0000-4000-8000-000000000001",
    session_id: sessionId,
    run_id: runId,
    tool_call_id: "call-web",
    tool_name: "web_search",
    action_family: "network",
    justification: "Chercher les informations de la veille",
    reason: "Network approval required",
    risks: ["network"],
    path: null,
    created_at: "2026-08-02T10:00:00Z",
  };
  const cron = {
    id: "cron-free-blocked", name: "Veille libre", schedule: "0 9 * * *",
    prompt: "Résume les nouvelles importantes", workspace: "/tmp/project",
    agent_id: "main", skills: [], security_mode: "limited",
    provider_id: "test", model: "test-model", reasoning: "medium", enabled: false,
    auto_resume: true, session_id: sessionId,
    notification_session_id: "33333333-3333-4333-8333-333333333333",
    next_run_at: null, last_run_at: null, last_status: "approval_pending",
    last_error: null, last_retryable: false, in_flight: false, blocked: true,
    workflow: null, workflow_revision: 0, workflow_basis_hash: null, workflow_updated_at: null,
  };
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/catalog")) {
      await route.fulfill({ json: {
        default_provider: "test",
        agents: [{ id: "main", description: "Main", provider: "test" }],
        skills: [{ name: "workflow-creator", description: "Conçoit les workflows" }],
        providers: [{ id: "test", kind: "test", models: ["test-model"] }],
        tools: [{ name: "web_search", description: "Recherche web", module: "web", risks: ["network"] }],
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons/cron-free-blocked/approval-status")) {
      await route.fulfill({ json: {
        approved_scopes: [], pending_count: 1, pending_run_id: runId,
      } });
      return true;
    }
    if (
      url.pathname.endsWith("/crons/cron-free-blocked/workflow")
      && route.request().method() === "GET"
    ) {
      workflowReadRequests += 1;
      await route.fulfill({ status: 404, json: { detail: "Not Found" } });
      return true;
    }
    if (url.pathname.endsWith("/crons/workflow-proposals") && route.request().method() === "POST") {
      proposalRequests += 1;
      await route.fulfill({ status: 404, json: { detail: "Not Found" } });
      return true;
    }
    if (url.pathname.endsWith("/approvals")) {
      await route.fulfill({ json: [approval] });
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
  await page.getByText("Veille libre", { exact: true }).click();

  await expect(page.getByText("Mode libre — sans workflow", { exact: true })).toBeVisible();
  await expect(page.getByText("1 autorisation pour 1 action attend ta décision.")).toBeVisible();
  expect(workflowReadRequests).toBe(0);
  await expect(page.getByText("Not Found", { exact: true })).toHaveCount(0);
  const prepareWorkflow = page.getByRole("button", { name: "Préparer un workflow guidé" });
  await expect(prepareWorkflow).toBeEnabled();
  await prepareWorkflow.click();

  await expect.poll(() => proposalRequests).toBe(1);
  await expect(page.getByText(/La proposition guidée n’est pas encore chargée/)).toHaveRole("alert");
  await expect(page.getByText(/Redémarre l’application puis réessaie/)).toBeVisible();
  await expect(page.getByText("Not Found", { exact: true })).toHaveCount(0);
});

test("workflow proposal provider errors are actionable and never expose technical details", async ({ page }) => {
  let proposalRequests = 0;
  const cron = {
    id: "cron-provider-error", name: "Veille du matin", schedule: "0 9 * * *",
    prompt: "Résume les nouvelles importantes", workspace: "/tmp/project",
    agent_id: "main", skills: ["workflow-creator"], security_mode: "limited",
    provider_id: "test", model: "test-model", reasoning: "medium", enabled: false,
    auto_resume: true, session_id: "11111111-1111-4111-8111-111111111111",
    notification_session_id: "33333333-3333-4333-8333-333333333333",
    next_run_at: null, last_run_at: null, last_status: null, last_error: null,
    last_retryable: false, in_flight: false, blocked: false, workflow: null,
    workflow_revision: 0, workflow_basis_hash: null, workflow_updated_at: null,
  };
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/catalog")) {
      await route.fulfill({ json: {
        default_provider: "test",
        agents: [{ id: "main", description: "Main", provider: "test" }],
        skills: [{ name: "workflow-creator", description: "Conçoit les workflows" }],
        providers: [{ id: "test", kind: "test", models: ["test-model"] }],
        tools: [{ name: "web_search", description: "Recherche web", module: "web", risks: ["network"] }],
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons/cron-provider-error/approval-status")) {
      await route.fulfill({ json: { approved_scopes: [], pending_count: 0, pending_run_id: null } });
      return true;
    }
    if (url.pathname.endsWith("/crons/workflow-proposals") && route.request().method() === "POST") {
      proposalRequests += 1;
      await route.fulfill({
        status: 502,
        json: {
          detail: "UnexpectedModelBehavior: status=401 model=gpt-provider-debug body={error:invalid_api_key}",
        },
      });
      return true;
    }
    if (url.pathname.endsWith("/approvals")) {
      await route.fulfill({ json: [] });
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
  await page.getByText("Veille du matin", { exact: true }).click();

  const workflowPanel = page.locator(".routine-workflow-panel");
  await workflowPanel.getByRole("button", { name: "Préparer un workflow guidé" }).click();

  await expect.poll(() => proposalRequests).toBe(1);
  await expect(workflowPanel.getByText(
    "Le modèle n’a pas pu préparer le workflow. Vérifie le fournisseur et le modèle configurés, puis réessaie.",
  )).toHaveRole("alert");
  await expect(workflowPanel).not.toContainText("UnexpectedModelBehavior");
  await expect(workflowPanel).not.toContainText("status=401");
  await expect(workflowPanel).not.toContainText("gpt-provider-debug");
  await expect(workflowPanel).not.toContainText("body=");
  await expect(workflowPanel).not.toContainText("invalid_api_key");
});

test("a saved routine workflow can be removed without changing prompt or skills", async ({ page }) => {
  let deleteRequests = 0;
  const workflow = {
    schema: "amk.workflow/v1", id: "routine-veille-v1", title: "Veille", status: "ready",
    execution: { mode: "agent_guided", deviation: "stop_and_report", timezone: "Europe/Paris" },
    missing_dependencies: [],
    steps: [{ id: "news", title: "Recherche", kind: "tool", tool: "web_search", needs: [] }],
  };
  const cron = {
    id: "cron-workflow", name: "Veille", schedule: "0 9 * * *", prompt: "Cherche les nouvelles",
    workspace: "/tmp/project", agent_id: "main", skills: ["daily-review"], security_mode: "limited",
    provider_id: "test", model: "test-model", reasoning: "medium", enabled: false,
    auto_resume: true, session_id: "11111111-1111-4111-8111-111111111111",
    notification_session_id: "33333333-3333-4333-8333-333333333333",
    next_run_at: null, last_run_at: null, last_status: null, last_error: null,
    last_retryable: false, in_flight: false, blocked: false, workflow,
    workflow_revision: 2, workflow_basis_hash: "basis", workflow_updated_at: "2026-08-02T10:00:00Z",
  };
  await page.unroute("**/api/kernel/**");
  await installKernelMock(page, async (route, url) => {
    if (url.pathname.endsWith("/catalog")) {
      await route.fulfill({ json: {
        default_provider: "test",
        agents: [{ id: "main", description: "Main", provider: "test" }],
        skills: [{ name: "daily-review", description: "Cadre la synthèse" }],
        providers: [{ id: "test", kind: "test", models: ["test-model"] }], tools: [],
      } });
      return true;
    }
    if (url.pathname.endsWith("/crons/cron-workflow/workflow") && route.request().method() === "GET") {
      await route.fulfill({ json: { workflow, basis_hash: "basis", revision: 2, updated_at: "2026-08-02T10:00:00Z" } });
      return true;
    }
    if (url.pathname.endsWith("/crons/cron-workflow/workflow") && route.request().method() === "DELETE") {
      deleteRequests += 1;
      await route.fulfill({ json: { id: "cron-workflow", status: "deleted", workflow: null, revision: 3 } });
      return true;
    }
    if (url.pathname.endsWith("/crons/cron-workflow/approval-status")) {
      await route.fulfill({ json: { approved_scopes: [], pending_count: 0, pending_run_id: null } });
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
  await page.getByText("Veille", { exact: true }).click();
  await expect(page.getByText("Workflow actif · v2")).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /daily-review/ })).toBeChecked();

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("conservera son prompt et ses skills");
    await dialog.dismiss();
  });
  await page.getByRole("button", { name: "Supprimer le workflow…" }).click();
  expect(deleteRequests).toBe(0);

  page.once("dialog", async (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Supprimer le workflow…" }).click();
  await expect(page.getByText("Workflow supprimé · la routine continuera avec son prompt et ses skills.")).toBeVisible();
  await expect(page.getByText("Mode libre — sans workflow", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Demande exécutée")).toHaveValue("Cherche les nouvelles");
  await expect(page.getByRole("checkbox", { name: /daily-review/ })).toBeChecked();
  expect(deleteRequests).toBe(1);
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
