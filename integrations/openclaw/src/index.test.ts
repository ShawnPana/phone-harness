import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import entry from "./index.js";

type Execute = (id: string, params: unknown, signal?: AbortSignal) => Promise<{
  content: Array<{ type: string; text?: string }>;
  details: Record<string, unknown>;
}>;
type Hook = (event: Record<string, unknown>) =>
  | undefined
  | {
      requireApproval: {
        title: string;
        description: string;
        allowedDecisions: string[];
        onResolution: (decision: string) => void;
      };
    };

let stateDir: string;
let callId = 0;

function load() {
  const run = vi.fn(async () => ({ stdout: "ran\n", stderr: "", code: 0, termination: "exit" }));
  const tools: Array<{ factory: (context: Record<string, unknown>) => unknown; options: unknown }> = [];
  const hooks: Record<string, Hook> = {};
  entry.register({
    runtime: {
      system: { runCommandWithTimeout: run },
      media: { resizeToJpeg: vi.fn(), getImageMetadata: vi.fn() },
      state: { resolveStateDir: () => stateDir },
    },
    registerTool: (factory: never, options: unknown) => tools.push({ factory, options }),
    on: (name: string, handler: Hook) => {
      hooks[name] = handler;
    },
  } as never);
  const tool = () => tools[0].factory({}) as { name: string; execute: Execute };
  return { run, tools, hooks, tool };
}

function nextId() {
  callId += 1;
  return `call-${callId}`;
}

function ask(hooks: Record<string, Hook>, toolCallId: string, code: string) {
  const asked = hooks.before_tool_call({ toolName: "phone_exec", toolCallId, params: { code } });
  if (!asked) {
    throw new Error("expected an approval request");
  }
  return asked.requireApproval;
}

function error(result: { content: Array<{ text?: string }> }) {
  return JSON.parse(result.content[0].text ?? "{}").error;
}

beforeEach(() => {
  stateDir = mkdtempSync(path.join(tmpdir(), "ph-openclaw-"));
});
afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

describe("registration", () => {
  it("adds one optional tool and the approval hook", () => {
    const { tools, hooks, tool } = load();
    expect(tools).toHaveLength(1);
    expect(tools[0].options).toEqual({ name: "phone_exec", optional: true });
    expect(tool().name).toBe("phone_exec");
    expect(Object.keys(hooks)).toEqual(["before_tool_call"]);
  });

  it("points the model at the skill and away from a shell", () => {
    const { description } = load().tool() as unknown as { description: string };
    expect(description).toContain("phone-harness skill");
    expect(description).toContain("through a shell");
  });

  it("does not give the tool to a sandboxed session, which it would escape", () => {
    expect(load().tools[0].factory({ sandboxed: true })).toBeNull();
  });
});

describe("approval", () => {
  it("asks before every script and offers no standing trust", () => {
    const approval = ask(load().hooks, nextId(), "print(1)");
    expect(approval.allowedDecisions).toEqual(["allow-once", "deny"]);
    expect(approval.title.length).toBeLessThanOrEqual(80);
    expect(approval.description).toContain("print(1)");
  });

  it("asks again for the same script", () => {
    const { hooks } = load();
    expect(ask(hooks, nextId(), "print(1)")).toBeTruthy();
    expect(ask(hooks, nextId(), "print(1)")).toBeTruthy();
  });

  it("leaves other tools alone", () => {
    const { hooks } = load();
    expect(hooks.before_tool_call({ toolName: "exec", params: { code: "x" } })).toBeUndefined();
  });

  it("does not ask about an empty script, which the tool refuses", async () => {
    const { hooks, tool, run } = load();
    const id = nextId();
    expect(hooks.before_tool_call({ toolName: "phone_exec", toolCallId: id, params: { code: " " } })).toBeUndefined();
    expect(error(await tool().execute(id, { code: " " }))).toContain("No code provided");
    expect(run).not.toHaveBeenCalled();
  });
});

describe("a script runs only if it was approved", () => {
  it("runs after allow-once", async () => {
    const { hooks, tool, run } = load();
    const id = nextId();
    ask(hooks, id, "print(1)").onResolution("allow-once");
    const result = await tool().execute(id, { code: "print(1)" });
    expect(result.details).toMatchObject({ ok: true });
    expect(run).toHaveBeenCalledOnce();
  });

  it("refuses when the hook never ran, so a load that missed it cannot run scripts", async () => {
    const { tool, run } = load();
    expect(error(await tool().execute(nextId(), { code: "print(1)" }))).toContain("not approved");
    expect(run).not.toHaveBeenCalled();
  });

  it.each(["deny", "timeout", "cancelled"])("refuses after %s", async (decision) => {
    const { hooks, tool, run } = load();
    const id = nextId();
    ask(hooks, id, "print(1)").onResolution(decision);
    expect(error(await tool().execute(id, { code: "print(1)" }))).toContain("not approved");
    expect(run).not.toHaveBeenCalled();
  });

  it("refuses a script other than the one the user saw", async () => {
    const { hooks, tool, run } = load();
    const id = nextId();
    ask(hooks, id, "print(1)").onResolution("allow-once");
    expect(error(await tool().execute(id, { code: "steal()" }))).toContain("not approved");
    expect(run).not.toHaveBeenCalled();
  });

  it("spends an approval on one run", async () => {
    const { hooks, tool, run } = load();
    const id = nextId();
    ask(hooks, id, "print(1)").onResolution("allow-once");
    await tool().execute(id, { code: "print(1)" });
    expect(error(await tool().execute(id, { code: "print(1)" }))).toContain("not approved");
    expect(run).toHaveBeenCalledOnce();
  });

  it("does not let one call's approval cover another call", async () => {
    const { hooks, tool, run } = load();
    ask(hooks, nextId(), "print(1)").onResolution("allow-once");
    expect(error(await tool().execute(nextId(), { code: "print(1)" }))).toContain("not approved");
    expect(run).not.toHaveBeenCalled();
  });

  it("works across two loads of the plugin, one holding the hook and one the tool", async () => {
    const holdsHook = load();
    const holdsTool = load();
    const id = nextId();
    ask(holdsHook.hooks, id, "print(1)").onResolution("allow-once");
    const result = await holdsTool.tool().execute(id, { code: "print(1)" });
    expect(result.details).toMatchObject({ ok: true });
  });
});

describe("the workspace", () => {
  function workspaceOf(run: ReturnType<typeof load>["run"]) {
    const [, options] = run.mock.calls[0] as unknown as [string[], { baseEnv: NodeJS.ProcessEnv }];
    return options.baseEnv.PH_AGENT_WORKSPACE;
  }

  it("is one folder in OpenClaw's state directory", async () => {
    const { hooks, tool, run } = load();
    const id = nextId();
    ask(hooks, id, "print(1)").onResolution("allow-once");
    await tool().execute(id, { code: "print(1)" });
    expect(workspaceOf(run)).toBe(path.join(stateDir, "phone-harness", "agent-workspace"));
  });

  it("honors an inherited PH_AGENT_WORKSPACE", async () => {
    vi.stubEnv("PH_AGENT_WORKSPACE", path.join(stateDir, "mine"));
    const { hooks, tool, run } = load();
    const id = nextId();
    ask(hooks, id, "print(1)").onResolution("allow-once");
    await tool().execute(id, { code: "print(1)" });
    expect(workspaceOf(run)).toBe(path.join(stateDir, "mine"));
  });

  it("expands ~ in an inherited PH_AGENT_WORKSPACE and ignores a blank one", async () => {
    vi.stubEnv("PH_AGENT_WORKSPACE", "~/phone-notes");
    const first = load();
    const id = nextId();
    ask(first.hooks, id, "print(1)").onResolution("allow-once");
    await first.tool().execute(id, { code: "print(1)" });
    expect(workspaceOf(first.run)).toBe(`${homedir()}/phone-notes`);

    vi.stubEnv("PH_AGENT_WORKSPACE", "   ");
    const second = load();
    const blankId = nextId();
    ask(second.hooks, blankId, "print(1)").onResolution("allow-once");
    await second.tool().execute(blankId, { code: "print(1)" });
    expect(workspaceOf(second.run)).toBe(path.join(stateDir, "phone-harness", "agent-workspace"));
  });
});

describe("the package", () => {
  const read = (name: string) => JSON.parse(readFileSync(new URL(`../${name}`, import.meta.url), "utf8"));
  const manifest = read("openclaw.plugin.json");
  const pkg = read("package.json");

  it("declares what the code registers", () => {
    expect(manifest.id).toBe(entry.id);
    expect(manifest.contracts.tools).toEqual(["phone_exec"]);
    expect(manifest.toolMetadata.phone_exec.optional).toBe(true);
  });

  it("loads at startup, so the approval hook is live before the first tool call", () => {
    expect(manifest.activation.onStartup).toBe(true);
  });

  it("publishes what ClawHub requires", () => {
    expect(manifest.version).toBe(pkg.version);
    expect(manifest.categories).toHaveLength(1);
    expect(pkg.private).toBeUndefined();
    expect(pkg.openclaw.extensions).toEqual(["./dist/index.js"]);
    expect(pkg.openclaw.compat.pluginApi).toBeTruthy();
    expect(pkg.openclaw.build.openclawVersion).toBeTruthy();
    expect(pkg.files).toEqual(expect.arrayContaining(["dist", "skills", "assets", "openclaw.plugin.json"]));
  });

  it("names itself under the owner it will publish as", () => {
    expect(pkg.name.startsWith("@phone-harness/")).toBe(true);
    expect(pkg.openclaw.install.clawhubSpec).toBe(pkg.name);
  });
});
