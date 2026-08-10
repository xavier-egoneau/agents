import { expect, test, type Page } from "@playwright/test";

type ChimeWindow = Window & { __chimeLog?: number[] };

async function installKernelMock(page: Page) {
  await page.route("**/api/kernel/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/runs") && route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      return route.fulfill({
        json: {
          session_id: body.session_id,
          run_id: "run-1",
          agent_id: "main",
          status: "success",
          output: "Réponse terminée",
          errors: [],
          artifacts: [],
        },
      });
    }
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
    if (url.pathname.endsWith("/sessions")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/approvals")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/crons")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/commands")) return route.fulfill({ json: [] });
    if (url.pathname.endsWith("/context-status")) {
      return route.fulfill({
        json: { context_window_tokens: null, estimated_history_tokens: 0, estimated_ratio: null },
      });
    }
    if (url.pathname.endsWith("/plans/current")) return route.fulfill({ json: null });
    return route.fulfill({ json: {} });
  });
}

test("a completed run schedules the done chime", async ({ page }) => {
  await page.addInitScript(() => {
    const win = window as ChimeWindow;
    win.__chimeLog = [];
    const original = OscillatorNode.prototype.start;
    OscillatorNode.prototype.start = function (this: OscillatorNode, when?: number) {
      win.__chimeLog?.push(this.frequency.value);
      return original.call(this, when);
    };
  });
  await installKernelMock(page);
  await page.goto("/");
  await page.waitForFunction(() =>
    window.localStorage.getItem("amk.composer.preferences.v1") !== null);

  await page.getByLabel("Message au kernel").fill("Bonjour");
  await page.getByLabel("Envoyer").click();
  await expect(page.getByText("Réponse terminée")).toBeVisible();

  await page.waitForFunction(() => (window as ChimeWindow).__chimeLog?.includes(660));
  const log = await page.evaluate(() => (window as ChimeWindow).__chimeLog ?? []);
  expect(log).toContain(660);
  expect(log).toContain(880);
});
