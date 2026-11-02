/**
 * Run one `phone_exec` script.
 *
 * The script goes on stdin to the pinned Phone Harness, which uvx fetches on first use and keeps
 * in its cache. Phone Harness runs it with the phone helpers pre-imported against the user's phone
 * and stdout comes back as the tool result. A screenshot saved during the call is attached. Each
 * call is a fresh interpreter; the phone and the agent workspace persist.
 *
 * Safety that lives here:
 * - The child environment drops credentials. Telemetry is Phone Harness's own, on by default
 *   like the CLI's, and DO_NOT_TRACK or `config set telemetry false` reach it unchanged.
 * - One script drives the phone at a time, across subagents in this process.
 * - A script past its timeout, or a cancelled call, is stopped with every process it started,
 *   including ones in their own session or process group. See stopRun.
 */
import { execFile, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
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
// Finding and stopping every process a script started.
//
// The runner leads its own process group, but a script can start processes that leave it: a new
// session or group, or a child whose parent exits so that it is re-parented away from the runner.
// Each run therefore tags its processes with RUN_MARKER, which every child inherits, and keeps a
// record of the processes it has seen under the runner. Stopping a run kills, in repeated rounds
// until none is left: the runner's process tree, every process carrying the marker, every process
// in the record that is still the same process, everything under those, and their process groups.

/** The environment variable that tags a run's processes. Its value is new for every run. */
export const RUN_MARKER = "PH_OPENCLAW_RUN";
const SAMPLE_MS = 500;
const KILL_ROUND_MS = 50;
const KILL_ROUNDS = 40;

type Proc = { pid: number; ppid: number; pgid: number; zombie: boolean; started: string };
type ProcessTable = Map<number, Proc>;

function run(file: string, args: string[]): Promise<string> {
  return new Promise((resolve) => {
    execFile(file, args, { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 }, (error, stdout) =>
      resolve(error ? "" : stdout),
    );
  });
}

/** Every process on this machine. Linux reads /proc, so it works without ps; elsewhere, ps. */
export async function processTable(): Promise<ProcessTable> {
  const table: ProcessTable = new Map();
  if (process.platform === "linux") {
    for (const name of await readdir("/proc").catch(() => [] as string[])) {
      if (!/^\d+$/.test(name)) continue;
      const stat = await readFile(`/proc/${name}/stat`, "utf8").catch(() => "");
      // pid (comm) state ppid pgrp session ... starttime is field 22. comm may hold spaces or parens.
      const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
      if (fields.length < 20) continue;
      table.set(Number(name), {
        pid: Number(name),
        ppid: Number(fields[1]),
        pgid: Number(fields[2]),
        zombie: fields[0] === "Z",
        started: fields[19] ?? "",
      });
    }
    return table;
  }
  const out = await run("ps", ["-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "stat=", "-o", "lstart="]);
  for (const line of out.split("\n")) {
    const match = /^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.*\S)\s*$/.exec(line);
    if (!match) continue;
    const [, pid, ppid, pgid, stat, started] = match as unknown as [string, string, string, string, string, string];
    table.set(Number(pid), {
      pid: Number(pid),
      ppid: Number(ppid),
      pgid: Number(pgid),
      zombie: stat.startsWith("Z"),
      started,
    });
  }
  return table;
}

/**
 * Processes whose environment carries this run's marker. Linux shows the environment of every
 * process the user owns. macOS shows it through `ps -E`, except for its own system binaries; the
 * record of processes seen under the runner covers those.
 */
export async function markedPids(marker: string): Promise<number[]> {
  const needle = `${RUN_MARKER}=${marker}`;
  const found: number[] = [];
  if (process.platform === "linux") {
    for (const name of await readdir("/proc").catch(() => [] as string[])) {
      if (!/^\d+$/.test(name)) continue;
      const environ = await readFile(`/proc/${name}/environ`, "latin1").catch(() => "");
      if (environ.split("\0").includes(needle)) found.push(Number(name));
    }
    return found;
  }
  const out = await run("ps", ["-A", "-E", "-ww", "-o", "pid=", "-o", "command="]);
  for (const line of out.split("\n")) {
    const match = /^\s*(\d+)\s(.*)$/.exec(line);
    if (match && ` ${match[2]} `.includes(` ${needle} `)) found.push(Number(match[1]));
  }
  return found;
}

function descendants(roots: Iterable<number>, table: ProcessTable): Set<number> {
  const children = new Map<number, number[]>();
  for (const proc of table.values()) {
    const siblings = children.get(proc.ppid) ?? [];
    siblings.push(proc.pid);
    children.set(proc.ppid, siblings);
  }
  const found = new Set<number>();
  const stack = [...roots];
  while (stack.length > 0) {
    for (const child of children.get(stack.pop() as number) ?? []) {
      if (found.has(child)) continue;
      found.add(child);
      stack.push(child);
    }
  }
  return found;
}

/** Remembers each process seen under the runner, with its start time, so a reused pid is never taken for it. */
export class RunRecord {
  readonly seen = new Map<number, string>();
  readonly rootPid: number;
  readonly marker: string;
  /** Whether the runner itself is still running. Its pid is only safe to target until it exits. */
  running: () => boolean = () => true;

  constructor(rootPid: number, marker: string) {
    this.rootPid = rootPid;
    this.marker = marker;
  }

  note(table: ProcessTable): void {
    for (const pid of descendants([this.rootPid], table)) {
      const proc = table.get(pid);
      if (proc && !this.seen.has(pid)) this.seen.set(pid, proc.started);
    }
  }

  /** The run's live processes, found before anything is killed so no child is orphaned first. */
  async live(table: ProcessTable): Promise<Set<number>> {
    const roots = new Set<number>([this.rootPid, ...(await markedPids(this.marker))]);
    for (const [pid, started] of this.seen) {
      if (table.get(pid)?.started === started) roots.add(pid);
    }
    const all = new Set<number>([...roots, ...descendants(roots, table)]);
    all.delete(process.pid);
    return new Set([...all].filter((pid) => table.has(pid) && !table.get(pid)?.zombie));
  }
}

/** Stops a run. True only once every process it started is confirmed gone. */
export type Stopper = (record: RunRecord) => Promise<boolean>;

/** Stop every process the run started. True once none is left; false if some remain when time runs out. */
export async function stopRun(record: RunRecord): Promise<boolean> {
  if (process.platform === "win32") return stopTreeWindows(record.rootPid, { ...windowsStopDeps, rootRunning: record.running });
  const deadline = Date.now() + KILL_WAIT_MS / 2;
  for (let round = 0; round < KILL_ROUNDS && Date.now() < deadline; round++) {
    const table = await processTable();
    record.note(table);
    const targets = await record.live(table);
    const ownGroup = table.get(process.pid)?.pgid;
    const groups = new Set<number>([record.rootPid]);
    for (const pid of targets) groups.add(table.get(pid)?.pgid ?? record.rootPid);
    for (const group of groups) {
      if (group <= 1 || group === ownGroup) continue;
      try {
        process.kill(-group, "SIGKILL");
      } catch {
        // No such group any more.
      }
    }
    for (const pid of targets) {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        // Already gone.
      }
    }
    if (targets.size === 0 && round > 0) return true;
    await new Promise((resolve) => setTimeout(resolve, KILL_ROUND_MS));
  }
  return false;
}

// Windows has no process groups or sessions to leave, and no environment of other processes to read.
// taskkill /T ends a process and the tree under it, found through parent pids.

type WinProc = { ppid: number; created: number };

/** What stopping on Windows needs from the system, so the logic can be tested anywhere. */
export type WindowsStopDeps = {
  /** pid -> parent pid and creation time for every process, or null when Windows cannot list them. */
  snapshot: () => Promise<Map<number, WinProc> | null>;
  /** Run taskkill and wait for it to finish. Its exit code, or null when it could not start. */
  taskkill: (args: string[]) => Promise<number | null>;
  /** End one process directly, the fallback when taskkill cannot. */
  terminate: (pid: number) => void;
  /** Whether the runner is still running. Its pid is never targeted after it exits. */
  rootRunning: () => boolean;
  deadlineMs: number;
  pauseMs: number;
};

/** taskkill exit codes that mean the process is gone: ended now, or not found. */
const TASKKILL_DONE = new Set([0, 128]);

async function windowsSnapshot(): Promise<Map<number, WinProc> | null> {
  const script =
    "Get-CimInstance Win32_Process | ForEach-Object { '{0} {1} {2}' -f $_.ProcessId, $_.ParentProcessId, " +
    "$(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }) }";
  const out = await run("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script]);
  const table = new Map<number, WinProc>();
  for (const line of out.split(/\r?\n/)) {
    const match = /^\s*(\d+)\s+(\d+)\s+(\d+)\s*$/.exec(line);
    if (match) table.set(Number(match[1]), { ppid: Number(match[2]), created: Number(match[3]) });
  }
  return table.size > 0 ? table : null;
}

/** The runner and every process under it, each with its creation time. A child must be no older than its parent, so a reused pid adopts no strangers. */
export function windowsTree(rootPid: number, table: Map<number, WinProc>): Map<number, number> {
  const tree = new Map<number, number>([[rootPid, table.get(rootPid)?.created ?? 0]]);
  const stack = [rootPid];
  while (stack.length > 0) {
    const parent = stack.pop() as number;
    const parentCreated = tree.get(parent) ?? 0;
    for (const [pid, proc] of table) {
      if (proc.ppid !== parent || tree.has(pid) || proc.created < parentCreated) continue;
      tree.set(pid, proc.created);
      stack.push(pid);
    }
  }
  return tree;
}

const windowsStopDeps: WindowsStopDeps = {
  snapshot: windowsSnapshot,
  taskkill: (args) =>
    new Promise((resolve) => {
      const child = spawn("taskkill", args, { stdio: "ignore", windowsHide: true });
      child.on("error", () => resolve(null));
      child.on("close", (code) => resolve(code));
    }),
  terminate: (pid) => process.kill(pid),
  rootRunning: () => true,
  deadlineMs: KILL_WAIT_MS / 2,
  pauseMs: 200,
};

/**
 * Stop a run on Windows. List the tree first, run taskkill /T on the runner and wait for it, then
 * check that the runner and every process that was under it are gone, retrying what is left. A pid
 * is only targeted while it still belongs to the same process. True only once all are gone.
 */
export async function stopTreeWindows(rootPid: number, deps: WindowsStopDeps): Promise<boolean> {
  const before = await deps.snapshot();
  const tree = before ? windowsTree(rootPid, before) : new Map<number, number>();
  tree.delete(rootPid); // the runner is checked through its own process handle
  tree.delete(process.pid);
  const deadline = Date.now() + deps.deadlineMs;
  let code: number | null = null;
  let first = true;
  for (;;) {
    if (first || deps.rootRunning()) code = await deps.taskkill(["/T", "/F", "/PID", String(rootPid)]);
    first = false;
    const now = tree.size > 0 ? await deps.snapshot() : null;
    const left = now ? [...tree].filter(([pid, created]) => now.get(pid)?.created === created).map(([pid]) => pid) : [];
    const listed = tree.size === 0 || now !== null;
    // With no list of the tree, only the runner can be checked, so taskkill must also report it done.
    const treeGone = listed && left.length === 0 && (before !== null || (code !== null && TASKKILL_DONE.has(code)));
    if (!deps.rootRunning() && treeGone) return true;
    if (Date.now() >= deadline) return false;
    for (const pid of left) {
      const ended = await deps.taskkill(["/T", "/F", "/PID", String(pid)]);
      if (ended === null) {
        try {
          deps.terminate(pid);
        } catch {
          // Gone, or not ours to end; the next check tells.
        }
      }
    }
    await new Promise((resolve) => setTimeout(resolve, deps.pauseMs));
  }
}

type ScriptExit = {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  aborted: boolean;
  spawnError?: NodeJS.ErrnoException;
  /** Set when the run was stopped: whether every process it started is confirmed gone. */
  stopConfirmed?: boolean;
  record?: RunRecord;
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
  stop: Stopper;
}): Promise<ScriptExit> {
  return new Promise((resolve) => {
    const out = tailCollector(OUTPUT_CAP);
    const err = tailCollector(STDERR_CAP);
    const marker = randomUUID();
    const child = spawn(command.file, command.args, {
      cwd: options.cwd,
      env: { ...options.env, [RUN_MARKER]: marker } as NodeJS.ProcessEnv,
      stdio: ["pipe", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    const record = child.pid === undefined ? undefined : new RunRecord(child.pid, marker);
    if (record) record.running = () => child.exitCode === null && child.signalCode === null;
    let timedOut = false;
    let aborted = false;
    let settled = false;
    let stopping = false;
    let killWait: NodeJS.Timeout | undefined;
    // Note who is under the runner while it runs, so a process orphaned later can still be found.
    const sampler =
      record && process.platform !== "win32"
        ? setInterval(() => {
            void processTable().then((table) => record.note(table));
          }, SAMPLE_MS)
        : undefined;

    let stopped: Promise<boolean> = Promise.resolve(true);

    const settle = (exitCode: number | null, spawnError?: NodeJS.ErrnoException) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(killWait);
      clearInterval(sampler);
      options.signal?.removeEventListener("abort", onAbort);
      const result = { exitCode, stdout: out.read(), stderr: err.read(), timedOut, aborted, spawnError, record };
      // A stopped run reports back only once its stop has finished, confirmed or not.
      void stopped.then((confirmed) => resolve(stopping ? { ...result, stopConfirmed: confirmed } : result));
    };
    const stop = () => {
      if (stopping) return;
      stopping = true;
      clearInterval(sampler);
      // Close follows the kill. Do not hang if a pipe stays open anyway.
      killWait = setTimeout(() => settle(null), KILL_WAIT_MS);
      if (record) stopped = options.stop(record).catch(() => false);
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
  stop?: Stopper;
  confirmEveryMs?: number;
};

const CONFIRM_EVERY_MS = 2_000;
const UNCONFIRMED =
  "Not every process the script started could be confirmed stopped, so the phone stays reserved until they are.";

/** Keep stopping a run until every process it started is confirmed gone. */
async function keepStopping(record: RunRecord, stop: Stopper, everyMs: number): Promise<void> {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, everyMs));
    if (await stop(record).catch(() => false)) return;
  }
}

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
  const stop = options.stop ?? stopRun;
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
      stop,
    });
  } catch (error) {
    lock.release();
    return failure(`Failed to launch phone-harness: ${(error as Error).message}`);
  }
  const unconfirmed = exit.stopConfirmed === false;
  if (unconfirmed && exit.record) {
    // Fail closed: the phone is free again only once every process the script started is gone.
    void keepStopping(exit.record, stop, options.confirmEveryMs ?? CONFIRM_EVERY_MS).then(() => lock.release());
  } else {
    lock.release();
  }

  if (exit.spawnError) {
    const missing = exit.spawnError.code === "ENOENT";
    return failure(missing ? UV_MISSING : `Failed to launch phone-harness: ${exit.spawnError.message}`);
  }
  if (exit.aborted) {
    return failure(`phone_exec was cancelled. ${unconfirmed ? UNCONFIRMED : "The script was stopped."}`, compact({ stopConfirmed: !unconfirmed }));
  }
  if (exit.timedOut) {
    return failure(
      `phone_exec timed out after ${timeoutS}s. Split the work into smaller scripts or raise timeout_s ` +
        `(max ${MAX_TIMEOUT_S}).${unconfirmed ? ` ${UNCONFIRMED}` : ""}`,
      { timedOut: true, timeoutMs: timeoutS * 1000, stopConfirmed: !unconfirmed },
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
