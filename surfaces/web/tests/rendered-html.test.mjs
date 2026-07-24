import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the AMK surface", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>AMK — Agentic Markdown Kernel<\/title>/i);
  assert.match(html, /Agentic kernel/);
  assert.match(html, /Projet actif/);
  assert.match(html, /Conversation active/);
  assert.doesNotMatch(html, /Your site is taking shape|Starter Project/);
});

test("keeps project selection and guardian approvals wired to the kernel", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(page, /amk\.workspaces/);
  assert.match(page, /workspaces\/current/);
  assert.match(page, /workspaces\/validate/);
  assert.match(page, /workspace: activeWorkspace/);
  assert.match(page, /approval_pending/);
  assert.match(page, /approvals\/\$\{approval\.approval_id\}\/resolve/);
  assert.match(page, /Autoriser/);
  assert.match(page, /Refuser/);
  assert.match(page, /ReactMarkdown/);
  assert.match(page, /remarkPlugins=\{\[remarkGfm\]\}/);
  assert.match(page, /MarkdownMessage content=\{message\.content\}/);
  assert.match(page, /api\/kernel\/sessions\?workspace=/);
  assert.match(page, /new EventSource\(`\/api\/kernel\/sessions\/\$\{sessionId\}\/events`\)/);
  assert.match(page, /traceEventsForRun\(traceEvents, message\.runId\)/);
  assert.match(page, /activeRunId === message\.runId/);
  assert.match(page, /useEffect\(\(\) => setExpanded\(live\), \[live\]\)/);
  assert.match(page, /Historique/);
  assert.match(page, /managementModal/);
  assert.match(page, /Configuration du contexte/);
  assert.match(page, /amk\.composer\.preferences\.v1/);
  assert.match(page, /securityMode, providerId, model: selectedModel, reasoning/);
});
