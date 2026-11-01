// register() against a stand-in for OpenClaw's plugin API. Needs `npm install`: unlike the other
// tests, this one loads the SDK entry helper and typebox.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import entry, { workspacePath } from "./index.ts";

type Factory = (context: Record<string, unknown>) => {
  name: string;
  executionMode?: string;
  catalogMode?: string;
  getExecutionTimeoutMs?: (params: unknown) => number;
  execute: (id: string, params: unknown, signal?: AbortSignal) => Promise<{ details: Record<string, unknown> }>;
} | null;

function registered(pluginConfig?: Record<string, unknown>) {
  const tools: Array<{ tool: Factory; options: { name?: string } }> = [];
  const hooks: Array<{ name: string; handler: unknown; options: { timeoutMs?: number } }> = [];
  const api = {
    pluginConfig,
    runtime: { state: { resolveStateDir: () => "/state" } },
    registerTool: (tool: Factory, options: { name?: string }) => tools.push({ tool, options }),
    on: (name: string, handler: unknown, options: { timeoutMs?: number }) => hooks.push({ name, handler, options }),
  };
  entry.register(api as never);
  return { tools, hooks };
}

const manifest = JSON.parse(readFileSync(new URL("../openclaw.plugin.json", import.meta.url), "utf8"));

describe("register()", () => {
  it("matches the manifest", () => {
    assert.equal(entry.id, manifest.id);
    const { tools } = registered();
    assert.deepEqual(tools.map(({ options }) => options.name), manifest.contracts.tools);
  });

  it("offers the tool to everyone but a sandboxed session", () => {
    const [{ tool }] = registered().tools as [{ tool: Factory }];
    assert.equal(tool({ senderIsOwner: true })?.name, "phone_exec");
    assert.equal(tool({ senderIsOwner: false })?.name, "phone_exec");
    assert.equal(tool({})?.name, "phone_exec");
    assert.equal(tool({})?.executionMode, "sequential");
    assert.equal(tool({ sandboxed: true, senderIsOwner: true }), null);
  });

  it("keeps the tool direct, so screenshots reach the model, and sets its own time budget", () => {
    const [{ tool }] = registered().tools as [{ tool: Factory }];
    assert.equal(tool({})?.catalogMode, "direct-only");
    assert.ok((tool({})?.getExecutionTimeoutMs?.({ timeout_s: 600 }) ?? 0) > 600_000);
  });

  it("refuses to run for a non-owner, and says why", async () => {
    const [{ tool }] = registered().tools as [{ tool: Factory }];
    // A real run would need uvx. This one must stop before it gets that far.
    const result = await tool({ senderIsOwner: false })?.execute("id", { code: "print(1)" });
    assert.equal(result?.details.ok, false);
    assert.match(String(result?.details.error), /owner/);
  });

  it("puts the gate in front of the tool, with room for a slow classifier", () => {
    const { hooks } = registered();
    assert.equal(hooks.length, 1);
    assert.equal(hooks[0]?.name, "before_tool_call");
    assert.ok((hooks[0]?.options.timeoutMs ?? 0) > 15_000);
  });
});

describe("the workspace", () => {
  it("is one folder in OpenClaw's state directory", () => {
    assert.equal(workspacePath({}, "/state"), join("/state", "phone-harness", "agent-workspace"));
  });

  it("follows an inherited PH_AGENT_WORKSPACE", () => {
    assert.equal(workspacePath({ PH_AGENT_WORKSPACE: "/mine" }, "/state"), "/mine");
    assert.equal(workspacePath({ PH_AGENT_WORKSPACE: "~/notes" }, "/state"), join(homedir(), "notes"));
    assert.equal(workspacePath({ PH_AGENT_WORKSPACE: "  " }, "/state"), join("/state", "phone-harness", "agent-workspace"));
  });
});
