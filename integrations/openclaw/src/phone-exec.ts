/**
 * Run one `phone_exec` script.
 *
 * The script goes on stdin to the pinned Phone Harness, which uvx fetches on first use and keeps
 * in its cache. Phone Harness runs it with the phone helpers pre-imported against the user's phone
 * and stdout comes back as the tool result. A screenshot saved during the call is attached. Each
 * call is a fresh interpreter; the phone and the agent workspace persist.
 *
 * Safety that lives here:
 * - The child environment drops credentials, and telemetry stays off unless the user opted in.
 * - One script drives the phone at a time, across subagents in this process.
 * - A script past its timeout, or a cancelled call, is killed with everything it started.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, join } from "node:path";

/** The Phone Harness release this plugin runs. To move to a new release, bump this one line. */
export const PHONE_HARNESS_VERSION = "0.3.3";

export const DEFAULT_TIMEOUT_S = 300;
export const MIN_TIMEOUT_S = 5;
export const MAX_TIMEOUT_S = 1800;
const LOCK_WAIT_MS = 120_000;
const KILL_WAIT_MS = 10_000;
const OUTPUT_CAP = 50_000;
const STDERR_CAP = 4_000;
/** OpenClaw shrinks tool images itself (agents.defaults.imageMaxDimensionPx). This only skips absurd files. */
const IMAGE_CAP_BYTES = 10 * 1024 * 1024;

const UV_MISSING =
  "Phone Harness runs through uv, and uvx is not on this Gateway's PATH. Install uv " +
  "(https://docs.astral.sh/uv/) on the machine that runs the Gateway, then try again.";
const BAD_SCREENSHOT =
  "That screenshot is not a usable image, so it is not attached. Take another, or read the screen with screen().";

export type Command = { file: string; args: string[] };
export type Env = Record<string, string | undefined>;

/** argv for the pinned Phone Harness. uvx keeps it apart from a `phone-harness` command on PATH. */
export function pinnedCommand(): Command {
  return {
    file: "uvx",
    args: ["--quiet", "--python", "3.12", "--from", `phone-harness==${PHONE_HARNESS_VERSION}`, "phone-harness"],
  };
}

// ---------------------------------------------------------------------------------------------
// The environment a script runs in.

/** Credentials by name. Everything else stays, so adb, iPhone Mirroring and a cloud phone keep working. */
const SECRET_NAME = /API_?KEY|TOKEN|SECRET|PASSW(OR)?D|CREDENTIAL|PRIVATE_?KEY|ACCESS_?KEY|COOKIE/i;

// Same enabling spellings as phone_harness.config's bool env override.
const TRUTHY = new Set(["1", "true", "yes", "on"]);

export function truthy(value: unknown): boolean {
  return typeof value === "string" ? TRUTHY.has(value.trim().toLowerCase()) : Boolean(value);
}

/** config.json, using the same directory rules as phone_harness.config.config_dir. */
function configJson(env: Env): string {
  if (env.PHONE_HARNESS_HOME) return join(env.PHONE_HARNESS_HOME, "config", "config.json");
  return join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "phone-harness", "config.json");
}

/**
 * True only for an explicit opt-in: `phone-harness config set telemetry true` (a value stored in
 * config.json), or PHONE_HARNESS_TELEMETRY set to an enabling value. The built-in default is on
 * for the CLI and is not an opt-in. An explicit false stays off.
 */
export function telemetryOptedIn(env: Env): boolean {
  const raw = env.PHONE_HARNESS_TELEMETRY;
  if (raw !== undefined) return truthy(raw);
  try {
    const data: unknown = JSON.parse(readFileSync(configJson(env), "utf8"));
    return typeof data === "object" && data !== null && "telemetry" in data && truthy(data.telemetry);
  } catch {
    return false;
  }
}

/**
 * Environment for the Phone Harness child.
 *
 * Starts from the Gateway's environment without OPENCLAW_* and anything named like a credential,
 * puts back any PHONE_HARNESS_* the user exported, and sets the workspace and client. uv's default
 * install folder goes on PATH because a Gateway service often runs with a short one.
 */
export function childEnv(base: Env, workspace: string): Env {
  const env: Env = {};
  for (const [key, value] of Object.entries(base)) {
    if (value === undefined) continue;
    if (key.startsWith("PHONE_HARNESS_") || !(key.startsWith("OPENCLAW_") || SECRET_NAME.test(key))) {
      env[key] = value;
    }
  }
  const pathKey = Object.keys(env).find((key) => key.toUpperCase() === "PATH") ?? "PATH";
  env[pathKey] = [env[pathKey], join(homedir(), ".local", "bin")].filter(Boolean).join(delimiter);
  env.PH_AGENT_WORKSPACE = workspace;
  env.PH_CLIENT = "openclaw";
  if (!telemetryOptedIn(base)) env.PHONE_HARNESS_TELEMETRY = "0";
  return env;
}

// ---------------------------------------------------------------------------------------------
// One script on the phone at a time (subagents in this process).

export class PhoneLock {
  private held = false;
  private readonly waiters: Array<() => void> = [];

  /** True once this caller holds the phone. False after waitMs, or when the call is cancelled. */
  acquire(waitMs: number, signal?: AbortSignal): Promise<boolean> {
    if (signal?.aborted) return Promise.resolve(false);
    if (!this.held) {
      this.held = true;
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const finish = (got: boolean) => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", giveUp);
        resolve(got);
      };
      const grant = () => finish(true);
      const giveUp = () => {
        this.waiters.splice(this.waiters.indexOf(grant), 1);
        finish(false);
      };
      const timer = setTimeout(giveUp, waitMs);
      signal?.addEventListener("abort", giveUp, { once: true });
      this.waiters.push(grant);
    });
  }

  release(): void {
    const next = this.waiters.shift();
    if (next) next();
    else this.held = false;
  }
}

const phoneLock = new PhoneLock();

// ---------------------------------------------------------------------------------------------
// The child process.

/** Kill the runner and everything it spawned (it leads its own process group). */
function killGroup(pid: number): void {
  if (process.platform === "win32") {
    spawn("taskkill", ["/T", "/F", "/PID", String(pid)], { stdio: "ignore" }).on("error", () => {});
    return;
  }
  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    // Already gone.
  }
}

type ScriptExit = {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  aborted: boolean;
  spawnError?: NodeJS.ErrnoException;
};

/** The last `cap` characters of a stream, without holding the rest in memory. */
function tailCollector(cap: number) {
  let text = "";
  return {
    add(chunk: string) {
      text += chunk;
      if (text.length > cap * 2) text = text.slice(-cap);
    },
    read: () => text.slice(-cap),
  };
}

function runChild(command: Command, code: string, options: {
  timeoutMs: number;
  cwd: string;
  env: Env;
  signal?: AbortSignal;
}): Promise<ScriptExit> {
  return new Promise((resolve) => {
    const out = tailCollector(OUTPUT_CAP);
    const err = tailCollector(STDERR_CAP);
    const child = spawn(command.file, command.args, {
      cwd: options.cwd,
      env: options.env as NodeJS.ProcessEnv,
      stdio: ["pipe", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let timedOut = false;
    let aborted = false;
    let settled = false;
    let killWait: NodeJS.Timeout | undefined;

    const settle = (exitCode: number | null, spawnError?: NodeJS.ErrnoException) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(killWait);
      options.signal?.removeEventListener("abort", onAbort);
      resolve({ exitCode, stdout: out.read(), stderr: err.read(), timedOut, aborted, spawnError });
    };
    const stop = () => {
      if (child.pid !== undefined) killGroup(child.pid);
      // Close follows the kill. Do not hang on a process that left the group and kept a pipe.
      killWait = setTimeout(() => settle(null), KILL_WAIT_MS);
    };
    const onAbort = () => {
      aborted = true;
      stop();
    };
    const timer = setTimeout(() => {
      timedOut = true;
      stop();
    }, options.timeoutMs);

    options.signal?.addEventListener("abort", onAbort, { once: true });
    child.stdout.setEncoding("utf8").on("data", out.add);
    child.stderr.setEncoding("utf8").on("data", err.add);
    child.on("error", (error) => settle(null, error));
    child.on("close", (exitCode) => settle(exitCode));
    child.stdin.on("error", () => {}); // a child that exits early closes stdin under us
    child.stdin.end(code);
  });
}

// ---------------------------------------------------------------------------------------------
// Screenshots.

// Absolute PNG / JPEG / WebP paths. A relative path, or a suffix of one, does not count.
const IMAGE_PATH = /(?<![\w.])((?:[A-Za-z]:[\\/]|\/)[^\s"']+?\.(?:png|jpe?g|webp))(?![\w.])/gi;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const JPEG_SIGNATURE = Buffer.from([0xff, 0xd8, 0xff]);

/**
 * Last absolute image path in stdout that exists and was modified during this call.
 * A one-second slack covers filesystems that store timestamps in whole seconds.
 */
export function findScreenshot(stdout: string, sinceMs: number): string | null {
  const paths = [...stdout.matchAll(IMAGE_PATH)].map((match) => match[1] as string);
  for (const path of paths.reverse()) {
    try {
      const stat = statSync(path);
      if (stat.isFile() && stat.mtimeMs >= sinceMs - 1000) return path;
    } catch {
      continue;
    }
  }
  return null;
}

type ImageContent = { type: "image"; data: string; mimeType: string };

/** What a file's first bytes say it is. The name proves nothing: a failed capture can leave text in a .png. */
function imageType(bytes: Buffer): string | null {
  if (bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) return "image/png";
  if (bytes.subarray(0, JPEG_SIGNATURE.length).equals(JPEG_SIGNATURE)) return "image/jpeg";
  const riff = bytes.subarray(0, 4).toString("latin1");
  return riff === "RIFF" && bytes.subarray(8, 12).toString("latin1") === "WEBP" ? "image/webp" : null;
}

/** The screenshot as a tool image, or null. A provider rejects a bad image along with the whole turn. */
function readImage(path: string): ImageContent | null {
  try {
    if (statSync(path).size > IMAGE_CAP_BYTES) return null;
    const bytes = readFileSync(path);
    const mimeType = imageType(bytes);
    return mimeType ? { type: "image", data: bytes.toString("base64"), mimeType } : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------------------------
// The tool.

type ToolContent = { type: "text"; text: string } | ImageContent;
export type ToolResult = { content: ToolContent[]; details: Record<string, unknown> };

function clampTimeout(value: unknown): number {
  const seconds = Math.trunc(Number(value ?? DEFAULT_TIMEOUT_S));
  if (!Number.isFinite(seconds) || seconds === 0) return DEFAULT_TIMEOUT_S;
  return Math.max(MIN_TIMEOUT_S, Math.min(seconds, MAX_TIMEOUT_S));
}

/**
 * The longest one call can take: waiting for the phone, the script, then stopping it. Without this
 * a harness applies its own default (90 s on Codex), shorter than the default script timeout.
 */
export function executionBudgetMs(params: PhoneExecParams): number {
  return LOCK_WAIT_MS + clampTimeout(params.timeout_s) * 1000 + KILL_WAIT_MS;
}

export function failure(message: string, details: Record<string, unknown> = {}): ToolResult {
  return {
    content: [{ type: "text", text: JSON.stringify({ success: false, error: message }) }],
    details: { ok: false, error: message, ...details },
  };
}

/** Drop empty fields, so the model reads only what happened. */
function compact(fields: Record<string, unknown>): Record<string, unknown> {
  const isEmpty = (value: unknown) =>
    value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
  return Object.fromEntries(Object.entries(fields).filter(([, value]) => !isEmpty(value)));
}

export type PhoneExecParams = { code?: unknown; timeout_s?: unknown };

export type PhoneExecOptions = {
  env: Env;
  workspace: string;
  command?: Command;
  lock?: PhoneLock;
  lockWaitMs?: number;
  signal?: AbortSignal;
};

export async function phoneExec(params: PhoneExecParams, options: PhoneExecOptions): Promise<ToolResult> {
  const code = typeof params.code === "string" ? params.code : "";
  if (!code.trim()) return failure("No code provided. Start with: ensure_device(); print(screen_info())");

  const { workspace } = options;
  const timeoutS = clampTimeout(params.timeout_s);
  const lock = options.lock ?? phoneLock;
  if (!(await lock.acquire(options.lockWaitMs ?? LOCK_WAIT_MS, options.signal))) {
    if (options.signal?.aborted) return failure("phone_exec was cancelled before it started.");
    return failure("The phone is busy with another phone_exec script. Try again when it finishes.");
  }
  const startedMs = Date.now();
  let exit: ScriptExit;
  try {
    // A missing workspace is seeded by Phone Harness on its first run, so it must not be created
    // here: Phone Harness leaves an existing directory alone, even an empty one.
    const cwd = existsSync(workspace) ? workspace : dirname(workspace);
    mkdirSync(cwd, { recursive: true });
    exit = await runChild(options.command ?? pinnedCommand(), code, {
      timeoutMs: timeoutS * 1000,
      cwd,
      env: childEnv(options.env, workspace),
      signal: options.signal,
    });
  } catch (error) {
    return failure(`Failed to launch phone-harness: ${(error as Error).message}`);
  } finally {
    lock.release();
  }

  if (exit.spawnError) {
    const missing = exit.spawnError.code === "ENOENT";
    return failure(missing ? UV_MISSING : `Failed to launch phone-harness: ${exit.spawnError.message}`);
  }
  if (exit.aborted) return failure("phone_exec was cancelled. The script was stopped.");
  if (exit.timedOut) {
    return failure(
      `phone_exec timed out after ${timeoutS}s. Split the work into smaller scripts or raise timeout_s ` +
        `(max ${MAX_TIMEOUT_S}).`,
      { timedOut: true, timeoutMs: timeoutS * 1000 },
    );
  }

  const shot = findScreenshot(exit.stdout, startedMs);
  const image = shot ? readImage(shot) : null;
  const ok = exit.exitCode === 0;
  const payload = compact({
    success: ok,
    exit_code: exit.exitCode,
    output: exit.stdout,
    stderr: exit.stderr.trim(),
    screenshots: shot ? [shot] : [],
    screenshot_error: shot && !image ? BAD_SCREENSHOT : "",
    workspace,
  });
  const content: ToolContent[] = [{ type: "text", text: JSON.stringify(payload) }];
  if (image) content.push(image);
  return {
    content,
    details: compact({ ok, exitCode: exit.exitCode, screenshots: shot ? [shot] : [], workspace }),
  };
}
