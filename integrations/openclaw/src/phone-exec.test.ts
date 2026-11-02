// The plumbing around a script, tested without OpenClaw or a phone. `node -` stands in for
// phone-harness: like it, node runs whatever script arrives on stdin.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, it } from "node:test";
import {
  childEnv,
  executionBudgetMs,
  findScreenshot,
  phoneExec,
  PHONE_HARNESS_VERSION,
  PhoneLock,
  pinnedCommand,
  stopRun,
  stopTreeWindows,
  windowsTree,
  type Env,
  type RunRecord,
  type ToolResult,
  type WindowsStopDeps,
} from "./phone-exec.ts";

const NODE = { file: process.execPath, args: ["-"] };
const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==",
  "base64",
);

let dir: string;
let workspace: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "ph-openclaw-"));
  workspace = join(dir, "state", "agent-workspace");
});

function run(code: string, extra: Record<string, unknown> = {}, env: Env = { PATH: process.env.PATH }) {
  return phoneExec({ code, ...extra }, { env, workspace, command: NODE, lock: new PhoneLock(), ...extra });
}

function payload(result: ToolResult): Record<string, unknown> {
  const first = result.content[0];
  assert.equal(first?.type, "text");
  return JSON.parse(first.type === "text" ? first.text : "{}");
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
  } catch {
    return false;
  }
  if (process.platform === "win32") return true;
  // A killed process can sit as a zombie until its new parent reaps it. It is not running.
  try {
    return !execFileSync("ps", ["-o", "stat=", "-p", String(pid)], { encoding: "utf8" }).trim().startsWith("Z");
  } catch {
    return false;
  }
}

async function waitFor(check: () => boolean, ms: number): Promise<boolean> {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (check()) return true;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return check();
}

function parentOf(pid: number): number {
  return Number(execFileSync("ps", ["-o", "ppid=", "-p", String(pid)], { encoding: "utf8" }).trim());
}

/**
 * A script that starts a process of every kind that can outlive a process-group kill: one in the
 * script's own group, one in its own session, and one orphaned at once by a launcher that exits.
 * With `seenFirst`, the orphan is a system binary whose launcher waits before exiting, so the
 * plugin sees it under the script before it is orphaned. Every pid goes to a file.
 */
function escapingScript(seenFirst = false) {
  const pids = join(dir, "pids");
  const forever = join(dir, "forever.js");
  const launcher = join(dir, "launcher.js");
  writeFileSync(forever, "setInterval(() => {}, 1000);\n");
  const orphan = seenFirst
    ? `spawn("/bin/sleep", ["60"], { stdio: "ignore", detached: true })`
    : `spawn(process.execPath, [${JSON.stringify(forever)}], { stdio: "ignore", detached: true })`;
  writeFileSync(
    launcher,
    `const { spawn } = require("node:child_process");
    const fs = require("node:fs");
    const child = ${orphan};
    fs.appendFileSync(${JSON.stringify(pids)}, "orphan " + child.pid + "\\nlauncher " + process.pid + "\\n");
    setTimeout(() => process.exit(0), ${seenFirst ? 1500 : 0});\n`,
  );
  const script = `
    const { spawn } = require("node:child_process");
    const fs = require("node:fs");
    const note = (name, pid) => fs.appendFileSync(${JSON.stringify(pids)}, name + " " + pid + "\\n");
    note("same-group", spawn(process.execPath, [${JSON.stringify(forever)}], { stdio: "ignore" }).pid);
    note("own-session", spawn(process.execPath, [${JSON.stringify(forever)}], { stdio: "ignore", detached: true }).pid);
    spawn(process.execPath, [${JSON.stringify(launcher)}], { stdio: "ignore", detached: true });
    setInterval(() => {}, 1000);
  `;
  const read = (): Record<string, number> => {
    const text = existsSync(pids) ? readFileSync(pids, "utf8") : "";
    return Object.fromEntries(text.trim().split("\n").filter(Boolean).map((line) => {
      const [name, pid] = line.split(" ");
      return [name, Number(pid)];
    }));
  };
  return { script, read, forever };
}

/** Wait until the script's processes exist and the orphan has lost its launcher. */
async function untilOrphaned(read: () => Record<string, number>): Promise<Record<string, number>> {
  assert.ok(await waitFor(() => Object.keys(read()).length === 4, 10_000), "the script started its processes");
  const pids = read();
  assert.ok(await waitFor(() => !alive(pids.launcher as number), 10_000), "the launcher exited");
  assert.notEqual(parentOf(pids.orphan as number), pids.launcher, "the orphan lost its parent");
  return pids;
}

describe("the pinned command", () => {
  it("runs one exact phone-harness release", () => {
    assert.match(PHONE_HARNESS_VERSION, /^\d+\.\d+\.\d+$/);
    const { file, args } = pinnedCommand();
    assert.equal(file, "uvx");
    assert.ok(args.includes(`phone-harness==${PHONE_HARNESS_VERSION}`));
    assert.equal(args.at(-1), "phone-harness");
  });
});

describe("a script", () => {
  it("returns what it printed", async () => {
    const result = await run("console.log('hello ' + (1 + 1))");
    assert.equal(result.details.ok, true);
    assert.equal(result.details.exitCode, 0);
    assert.deepEqual(payload(result), { success: true, exit_code: 0, output: "hello 2\n", workspace });
  });

  it("reports a failure with its exit code and the stderr tail", async () => {
    const result = await run("console.error('boom'); process.exit(3)");
    assert.equal(result.details.ok, false);
    assert.equal(result.details.exitCode, 3);
    assert.equal(payload(result).stderr, "boom");
  });

  it("refuses empty code without starting anything", async () => {
    const result = await phoneExec(
      { code: "   " },
      { env: {}, workspace, command: { file: "/nonexistent", args: [] } },
    );
    assert.equal(result.details.ok, false);
    assert.match(String(result.details.error), /ensure_device/);
  });

  it("explains a missing uv", async () => {
    const result = await phoneExec(
      { code: "print(1)" },
      { env: { PATH: dir }, workspace, command: { file: "ph-no-such-command", args: [] } },
    );
    assert.equal(result.details.ok, false);
    assert.match(String(result.details.error), /Install uv/);
  });

  it("caps output at the tail", async () => {
    const result = await run("console.log('x'.repeat(200000) + 'END')");
    const output = String(payload(result).output);
    assert.equal(output.length, 50_000);
    assert.ok(output.endsWith("END\n"));
  });

  it("leaves the workspace for phone-harness to seed, then runs inside it", async () => {
    await run("console.log(process.cwd())");
    assert.equal(existsSync(workspace), false);
    assert.ok(existsSync(join(dir, "state")));
    mkdirSync(workspace);
    const result = await run("console.log(process.cwd())");
    assert.match(String(payload(result).output), /agent-workspace/);
  });
});

const POSIX = process.platform !== "win32";
const NOT_POSIX = !POSIX && "sessions and process groups are POSIX";

describe("a script past its timeout", () => {
  it("is stopped with every process it started, wherever it went", { skip: NOT_POSIX, timeout: 60_000 }, async () => {
    const { script, read } = escapingScript();
    const result = await run(script, { timeout_s: 1 }); // clamped up to the 5 s minimum
    assert.equal(result.details.ok, false);
    assert.equal(result.details.timedOut, true);
    assert.equal(result.details.timeoutMs, 5000);
    const pids = read();
    assert.deepEqual(Object.keys(pids).sort(), ["launcher", "orphan", "own-session", "same-group"]);
    for (const [name, pid] of Object.entries(pids)) assert.equal(alive(pid), false, `${name} survived`);
  });

  it("gets a tool budget longer than the script's own timeout", () => {
    assert.ok(executionBudgetMs({}) > 300_000);
    assert.ok(executionBudgetMs({ timeout_s: 1800 }) > 1_800_000);
    assert.ok(executionBudgetMs({ timeout_s: 99_999 }) < 2_000_000); // clamped like the script
  });

  it("stops when the call is cancelled", { timeout: 30_000 }, async () => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 200);
    const started = Date.now();
    const result = await run("setInterval(() => {}, 1000)", { signal: controller.signal });
    assert.equal(result.details.ok, false);
    assert.match(String(result.details.error), /cancelled/);
    // Windows lists processes through PowerShell before and after taskkill, which takes longer.
    assert.ok(Date.now() - started < (process.platform === "win32" ? 15_000 : 5000));
  });
});

describe("a cancelled call", () => {
  it("stops a child in its own session, and one orphaned at once", { skip: NOT_POSIX, timeout: 60_000 }, async () => {
    const { script, read } = escapingScript();
    const controller = new AbortController();
    const running = run(script, { signal: controller.signal });
    const pids = await untilOrphaned(read);
    for (const [name, pid] of Object.entries(pids)) {
      if (name !== "launcher") assert.equal(alive(pid), true, `${name} was running`);
    }
    controller.abort();
    const result = await running;
    assert.match(String(result.details.error), /cancelled/);
    for (const [name, pid] of Object.entries(pids)) assert.equal(alive(pid), false, `${name} survived`);
  });

  it("stops a system binary orphaned after the plugin saw it", { skip: NOT_POSIX, timeout: 60_000 }, async () => {
    // macOS hides the environment of its own binaries, such as /bin/sleep, from other processes,
    // so here the record of processes seen under the script is what finds the orphan.
    const { script, read } = escapingScript(true);
    const controller = new AbortController();
    const running = run(script, { signal: controller.signal });
    const pids = await untilOrphaned(read);
    assert.equal(alive(pids.orphan as number), true);
    controller.abort();
    await running;
    for (const [name, pid] of Object.entries(pids)) assert.equal(alive(pid), false, `${name} survived`);
  });

  it("leaves processes the script did not start alone", { skip: NOT_POSIX, timeout: 60_000 }, async () => {
    const { script, read, forever } = escapingScript();
    const bystander = spawn(process.execPath, [forever], { stdio: "ignore", detached: true });
    try {
      const controller = new AbortController();
      const running = run(script, { signal: controller.signal });
      await untilOrphaned(read);
      controller.abort();
      await running;
      assert.equal(alive(bystander.pid as number), true, "a process outside the run was killed");
    } finally {
      bystander.kill("SIGKILL");
    }
  });
});

describe("an unconfirmed stop", () => {
  it("keeps the phone reserved until every process is confirmed gone", { timeout: 60_000 }, async () => {
    const lock = new PhoneLock();
    let calls = 0;
    // The real stop runs, but the first two report that something may be left.
    const stop = async (record: RunRecord) => {
      await stopRun(record);
      calls += 1;
      return calls > 2;
    };
    const controller = new AbortController();
    setTimeout(() => controller.abort(), 200);
    const result = await phoneExec(
      { code: "setInterval(() => {}, 1000)" },
      { env: { PATH: process.env.PATH }, workspace, command: NODE, lock, signal: controller.signal, stop, confirmEveryMs: 50 },
    );
    assert.match(String(result.details.error), /could be confirmed stopped, so the phone stays reserved/);
    assert.equal(result.details.stopConfirmed, false);
    assert.equal(await lock.acquire(0), false, "the phone was freed before the stop was confirmed");
    let freed = false;
    for (let i = 0; i < 100 && !freed; i++) {
      freed = await lock.acquire(0);
      if (!freed) await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(freed, true, "the phone was not freed once the stop was confirmed");
    assert.ok(calls >= 3);
  });
});

describe("stopping on Windows", () => {
  // The logic, with a stand-in for Windows, so it is tested on every platform.
  type Fake = { procs: Map<number, { ppid: number; created: number }>; calls: string[]; deps: WindowsStopDeps };

  function fake(options: { dies?: (pid: number) => boolean; taskkillCode?: number | null; list?: boolean } = {}): Fake {
    const procs = new Map([
      [100, { ppid: 1, created: 10 }], // the runner
      [101, { ppid: 100, created: 20 }],
      [102, { ppid: 101, created: 30 }],
      [200, { ppid: 1, created: 5 }], // not part of the run
    ]);
    const calls: string[] = [];
    const dies = options.dies ?? (() => true);
    const deps: WindowsStopDeps = {
      snapshot: async () => {
        calls.push("snapshot");
        return options.list === false ? null : new Map(procs);
      },
      taskkill: async (args) => {
        await new Promise((resolve) => setTimeout(resolve, 20));
        const pid = Number(args.at(-1));
        calls.push(`taskkill ${args.join(" ")}`);
        const tree = args.includes("/T") ? [...windowsTree(pid, procs).keys()] : [pid];
        for (const each of tree) if (dies(each)) procs.delete(each);
        return options.taskkillCode === undefined ? 0 : options.taskkillCode;
      },
      terminate: (pid) => {
        calls.push(`terminate ${pid}`);
        if (dies(pid)) procs.delete(pid);
      },
      rootRunning: () => procs.has(100),
      deadlineMs: 300,
      pauseMs: 10,
    };
    return { procs, calls, deps };
  }

  it("waits for taskkill, then confirms the tree is gone", async () => {
    const { procs, calls, deps } = fake();
    assert.equal(await stopTreeWindows(100, deps), true);
    assert.deepEqual(calls, ["snapshot", "taskkill /T /F /PID 100", "snapshot"]);
    assert.deepEqual([...procs.keys()], [200], "only the process outside the run is left");
  });

  it("goes after a process left under the runner, and fails closed if it will not go", async () => {
    const stubborn = fake({ dies: (pid) => pid !== 102 });
    assert.equal(await stopTreeWindows(100, stubborn.deps), false);
    assert.ok(stubborn.calls.includes("taskkill /T /F /PID 102"));
    let hits = 0;
    const late = fake({ dies: (pid) => pid !== 102 || ++hits > 1 }); // 102 survives the first taskkill only
    assert.equal(await stopTreeWindows(100, late.deps), true);
    assert.ok(late.calls.includes("taskkill /T /F /PID 102"));
  });

  it("never targets a pid that now belongs to a different process", async () => {
    const { procs, calls, deps } = fake({ dies: (pid) => pid !== 102 });
    deps.taskkill = async (args) => {
      calls.push(`taskkill ${args.join(" ")}`);
      for (const pid of [100, 101, 102]) procs.delete(pid);
      procs.set(102, { ppid: 1, created: 99 }); // 102 ended and was reused by a stranger
      return 0;
    };
    assert.equal(await stopTreeWindows(100, deps), true);
    assert.deepEqual(calls.filter((call) => call.startsWith("taskkill")), ["taskkill /T /F /PID 100"]);
  });

  it("ends a process directly when taskkill cannot start", async () => {
    const { procs, calls, deps } = fake({ taskkillCode: null, dies: (pid) => pid === 100 });
    deps.terminate = (pid) => {
      calls.push(`terminate ${pid}`);
      procs.delete(pid);
    };
    assert.equal(await stopTreeWindows(100, deps), true);
    assert.ok(calls.includes("terminate 101") && calls.includes("terminate 102"));
  });

  it("without a process list, needs taskkill to report the runner done", async () => {
    assert.equal(await stopTreeWindows(100, fake({ list: false }).deps), true);
    assert.equal(await stopTreeWindows(100, fake({ list: false, taskkillCode: 1 }).deps), false);
  });

  it("does not adopt a stranger whose parent pid was reused", () => {
    const table = new Map([
      [100, { ppid: 1, created: 10 }],
      [300, { ppid: 100, created: 3 }], // older than 100, so its parent was an earlier holder of the pid
      [301, { ppid: 100, created: 11 }],
    ]);
    assert.deepEqual([...windowsTree(100, table).keys()].sort(), [100, 301]);
  });

  it("on Windows, waits for taskkill and checks the tree is gone before freeing the phone", {
    skip: process.platform !== "win32" && "a real Windows run",
    timeout: 60_000,
  }, async () => {
    const pids = join(dir, "pids");
    const forever = join(dir, "forever.js");
    const middle = join(dir, "middle.js");
    writeFileSync(forever, "setInterval(() => {}, 1000);\n");
    writeFileSync(middle, `
      const child = require("node:child_process").spawn(process.execPath, [${JSON.stringify(forever)}], { stdio: "ignore" });
      require("node:fs").appendFileSync(${JSON.stringify(pids)}, "grandchild " + child.pid + "\\n");
      setInterval(() => {}, 1000);
    `);
    const script = `
      const child = require("node:child_process").spawn(process.execPath, [${JSON.stringify(middle)}], { stdio: "ignore" });
      require("node:fs").appendFileSync(${JSON.stringify(pids)}, "child " + child.pid + "\\n");
      setInterval(() => {}, 1000);
    `;
    const read = () => (existsSync(pids) ? readFileSync(pids, "utf8").trim().split("\n").filter(Boolean) : []);
    const lock = new PhoneLock();
    const controller = new AbortController();
    const running = phoneExec(
      { code: script },
      { env: { ...process.env }, workspace, command: NODE, lock, signal: controller.signal },
    );
    assert.ok(await waitFor(() => read().length === 2, 20_000), "the script started its processes");
    controller.abort();
    const result = await running;
    assert.match(String(result.details.error), /cancelled\. The script was stopped/);
    for (const line of read()) {
      const [name, pid] = line.split(" ");
      assert.equal(alive(Number(pid)), false, `${name} survived`);
    }
    assert.equal(await lock.acquire(0), true, "the phone was not freed");
  });
});

describe("one script at a time", () => {
  it("runs a second script only after the first", async () => {
    const lock = new PhoneLock();
    const log = join(dir, "log");
    const script = (name: string) => `
      const fs = require("node:fs");
      fs.appendFileSync(${JSON.stringify(log)}, "${name}-start\\n");
      setTimeout(() => fs.appendFileSync(${JSON.stringify(log)}, "${name}-end\\n"), 150);
    `;
    const options = { env: { PATH: process.env.PATH }, workspace, command: NODE, lock };
    await Promise.all([phoneExec({ code: script("a") }, options), phoneExec({ code: script("b") }, options)]);
    assert.deepEqual(readFileSync(log, "utf8").trim().split("\n"), ["a-start", "a-end", "b-start", "b-end"]);
  });

  it("reports the phone as busy after the wait, and frees it afterwards", async () => {
    const lock = new PhoneLock();
    assert.equal(await lock.acquire(0), true);
    const busy = await phoneExec(
      { code: "console.log(1)" },
      { env: {}, workspace, command: NODE, lock, lockWaitMs: 20 },
    );
    assert.match(String(busy.details.error), /busy/);
    lock.release();
    assert.equal(await lock.acquire(0), true);
  });

  it("frees the phone when a script fails to start", async () => {
    const lock = new PhoneLock();
    const options = { env: {}, workspace, lock, command: { file: "ph-no-such-command", args: [] } };
    await phoneExec({ code: "x" }, options);
    assert.equal(await lock.acquire(0), true);
  });

  it("a cancelled waiter gives up its place", async () => {
    const lock = new PhoneLock();
    await lock.acquire(0);
    const controller = new AbortController();
    const waiting = lock.acquire(60_000, controller.signal);
    controller.abort();
    assert.equal(await waiting, false);
    lock.release();
    assert.equal(await lock.acquire(0), true);
  });
});

describe("the script's environment", () => {
  const base: Env = {
    PATH: "/usr/bin",
    HOME: "/home/u",
    ANDROID_HOME: "/sdk",
    SSH_AUTH_SOCK: "/tmp/agent",
    OPENAI_API_KEY: "k",
    ANTHROPIC_APIKEY: "k",
    SLACK_BOT_TOKEN: "t",
    GH_TOKEN: "t",
    AWS_SECRET_ACCESS_KEY: "k",
    AWS_SESSION_TOKEN: "t",
    MY_PASSWORD: "p",
    GOOGLE_APPLICATION_CREDENTIALS: "/c.json",
    OPENCLAW_GATEWAY_TOKEN: "t",
    OPENCLAW_GATEWAY_PASSWORD: "p",
    OPENCLAW_STATE_DIR: "/s",
    PHONE_HARNESS_API_KEY: "mine",
    PHONE_HARNESS_PLATFORM: "android",
  };

  it("drops credentials and OpenClaw's own settings", () => {
    const env = childEnv(base, "/w");
    for (const key of [
      "OPENAI_API_KEY", "ANTHROPIC_APIKEY", "SLACK_BOT_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY",
      "AWS_SESSION_TOKEN", "MY_PASSWORD", "GOOGLE_APPLICATION_CREDENTIALS", "OPENCLAW_GATEWAY_TOKEN",
      "OPENCLAW_GATEWAY_PASSWORD", "OPENCLAW_STATE_DIR",
    ]) {
      assert.equal(env[key], undefined, key);
    }
  });

  it("keeps what the phone needs and what the user exported for phone-harness", () => {
    const env = childEnv(base, "/w");
    assert.equal(env.HOME, "/home/u");
    assert.equal(env.ANDROID_HOME, "/sdk");
    assert.equal(env.SSH_AUTH_SOCK, "/tmp/agent");
    assert.equal(env.PHONE_HARNESS_API_KEY, "mine");
    assert.equal(env.PHONE_HARNESS_PLATFORM, "android");
    assert.ok(String(env.PATH).startsWith("/usr/bin"));
    assert.ok(String(env.PATH).includes(join(".local", "bin")));
  });

  it("names the client and the workspace, and never picks a phone", () => {
    const env = childEnv({ PATH: "/usr/bin" }, "/w");
    assert.equal(env.PH_CLIENT, "openclaw");
    assert.equal(env.PH_AGENT_WORKSPACE, "/w");
    assert.equal(env.PHONE_HARNESS_PLATFORM, undefined);
  });

  it("reaches the script", async () => {
    const result = await run(
      "console.log(JSON.stringify([process.env.PH_CLIENT, process.env.OPENAI_API_KEY ?? null]))",
      {},
      { PATH: process.env.PATH, OPENAI_API_KEY: "k" },
    );
    assert.deepEqual(JSON.parse(String(payload(result).output)), ["openclaw", null]);
  });
});

describe("telemetry", () => {
  it("leaves the decision to Phone Harness, which defaults to on", () => {
    assert.equal(childEnv({ PATH: "/usr/bin" }, "/w").PHONE_HARNESS_TELEMETRY, undefined);
  });

  it("passes every opt-out through", () => {
    const env = childEnv({ DO_NOT_TRACK: "1", PHONE_HARNESS_TELEMETRY: "0", PHONE_HARNESS_HOME: "/h" }, "/w");
    assert.equal(env.DO_NOT_TRACK, "1");
    assert.equal(env.PHONE_HARNESS_TELEMETRY, "0");
    assert.equal(env.PHONE_HARNESS_HOME, "/h");
  });

  it("does not depend on the config file: Phone Harness reads it", () => {
    assert.equal(childEnv({ XDG_CONFIG_HOME: "/nowhere" }, "/w").PHONE_HARNESS_TELEMETRY, undefined);
  });
});

describe("screenshots", () => {
  it("attaches an image written during the call", async () => {
    const shot = join(dir, "shot.png");
    const script = `
      require("node:fs").writeFileSync(${JSON.stringify(shot)}, Buffer.from(${JSON.stringify(PIXEL.toString("base64"))}, "base64"));
      console.log(${JSON.stringify(`saved ${shot}`)});
    `;
    const result = await run(script);
    assert.deepEqual(result.details.screenshots, [shot]);
    assert.deepEqual(payload(result).screenshots, [shot]);
    assert.deepEqual(result.content[1], { type: "image", data: PIXEL.toString("base64"), mimeType: "image/png" });
  });

  // A real failure: a cloud phone's screencap crashed and printed this with a clean exit, and
  // the capture was saved as a .png. Attached as an image, it failed the whole model turn.
  it("never attaches a .png that is not an image, and says so", async () => {
    const shot = join(dir, "shot.png");
    const script = `
      require("node:fs").writeFileSync(${JSON.stringify(shot)}, "gralloc-mapper is missing\\nAborted (core dumped)\\n");
      console.log(${JSON.stringify(shot)});
    `;
    const result = await run(script);
    assert.equal(result.details.ok, true);
    assert.equal(result.content.length, 1);
    assert.deepEqual(payload(result).screenshots, [shot]);
    assert.match(String(payload(result).screenshot_error), /not a usable image/);
  });

  it("goes by the bytes for the image type, not the file name", async () => {
    const shot = join(dir, "really-a-png.jpg");
    writeFileSync(shot, PIXEL);
    const result = await run(`
      require("node:fs").utimesSync(${JSON.stringify(shot)}, new Date(), new Date());
      console.log(${JSON.stringify(shot)});
    `);
    assert.equal(result.content[1]?.type === "image" && result.content[1].mimeType, "image/png");
    assert.equal(payload(result).screenshot_error, undefined);
  });

  it("recognizes JPEG and WebP, and refuses an empty file", async () => {
    const cases: Array<[string, Buffer, string | undefined]> = [
      ["a.jpeg", Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0]), "image/jpeg"],
      ["b.webp", Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(4), Buffer.from("WEBPVP8 ")]), "image/webp"],
      ["c.png", Buffer.alloc(0), undefined],
    ];
    for (const [name, bytes, mimeType] of cases) {
      const shot = join(dir, name);
      writeFileSync(shot, bytes);
      const result = await run(`
        require("node:fs").utimesSync(${JSON.stringify(shot)}, new Date(), new Date());
        console.log(${JSON.stringify(shot)});
      `);
      const image = result.content[1];
      assert.equal(image?.type === "image" ? image.mimeType : undefined, mimeType, name);
    }
  });

  it("ignores an old file, a missing file and a relative path", () => {
    const old = join(dir, "old.png");
    writeFileSync(old, PIXEL);
    utimesSync(old, new Date(0), new Date(0));
    const since = Date.now();
    assert.equal(findScreenshot(`${old} ${join(dir, "missing.png")} relative/shot.png`, since), null);
  });

  it("takes the last usable path in the output", () => {
    const first = join(dir, "first.png");
    const second = join(dir, "second.jpg");
    writeFileSync(first, PIXEL);
    writeFileSync(second, PIXEL);
    const since = Date.now();
    assert.equal(findScreenshot(`${first}\n${second}\n`, since), second);
    assert.equal(findScreenshot(`${second}\n${first}\n`, since), first);
  });

  it("keeps the result as text when there is no image", async () => {
    const result = await run("console.log('no picture')");
    assert.equal(result.content.length, 1);
  });
});
