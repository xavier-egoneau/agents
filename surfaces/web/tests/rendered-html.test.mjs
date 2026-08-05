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
  assert.match(html, /<main class="shell"/);
  assert.doesNotMatch(html, /Your site is taking shape|Starter Project/);
});

test("routine workflow cards keep their natural height and wrap readable step details", async () => {
  const styles = await Promise.all([
    "../app/theme/components-modal.css",
    "../app/theme/components-workflow.css",
  ].map((path) => readFile(new URL(path, import.meta.url), "utf8"))).then((files) => files.join("\n"));
  const ruleFor = (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const match = styles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
    assert.ok(match, `Missing CSS rule for ${selector}`);
    return match[1];
  };

  assert.match(ruleFor(".resource-editor.cron-editor"), /display:\s*flex/);
  assert.match(ruleFor(".cron-editor > *"), /flex:\s*0\s+0\s+auto/);
  assert.match(ruleFor(".routine-workflow-step-content > p"), /white-space:\s*normal/);

  const argumentRule = ruleFor(".routine-workflow-step-args code");
  assert.match(argumentRule, /white-space:\s*pre-wrap/);
  assert.match(argumentRule, /overflow-wrap:\s*anywhere/);
  assert.doesNotMatch(argumentRule, /text-overflow:\s*ellipsis|white-space:\s*nowrap/);
});
