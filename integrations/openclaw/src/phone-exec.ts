/**
 * Running one phone script: the pinned command, one script at a time, and what comes back.
 * Nothing here decides whether a script may run; index.ts owns that.
 */
import { mkdirSync, statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { childEnv } from "./child-env.js";
import { PHONE_HARNESS_VERSION } from "./pin.js";

export const DEFAULT_TIMEOUT_S = 300;
export const MIN_TIMEOUT_S = 5;
export const MAX_TIMEOUT_S = 1800;
export const LOCK_WAIT_MS = 120_000;
const OUTPUT_CAP = 50_000;
const STDERR_CAP = 4_000;

// Tool results carry images at or below OpenClaw's default image limit, so the host does not
// scale the screenshot a second time.
const IMAGE_MAX_SIDE = 1200;
const IMAGE_QUALITY = 80;

// The Gateway caps approval text. A preview that fits shows the head and the tail, so a padded
// start cannot hide what the script ends with, and the count says how much was left out.
export const DESCRIPTION_MAX = 512;
const PREVIEW_INTRO = "Runs as Python on this computer and drives your phone: ";
const PREVIEW_MAX = DESCRIPTION_MAX - PREVIEW_INTRO.length;
const HIDDEN_MARKER_ROOM = 32;

export function describeScript(code: string): string {
  const compact = code.split(/\s+/).join(" ").trim();
  if (compact.length <= PREVIEW_MAX) {
    return PREVIEW_INTRO + compact;
  }
  const keep = PREVIEW_MAX - HIDDEN_MARKER_ROOM;
  const head = compact.slice(0, Math.ceil(keep * 0.7));
  const tail = compact.slice(compact.length - Math.floor(keep * 0.3));
  const hidden = compact.length - head.length - tail.length;
  return `${PREVIEW_INTRO}${head} …[+${hidden} chars hidden]… ${tail}`;
}

/** argv for the pinned release. uvx keeps it apart from a phone-harness on PATH. */
export function phoneCommand(): string[] {
  return [
    "uvx",
    "--quiet",
    "--python",
    "3.12",
    "--from",
    `phone-harness==${PHONE_HARNESS_VERSION}`,
    "phone-harness",
  ];
}

export function clampTimeoutS(value: unknown): number {
  const seconds = Math.trunc(Number(value ?? DEFAULT_TIMEOUT_S));
  if (!Number.isFinite(seconds) || seconds === 0) {
    return DEFAULT_TIMEOUT_S;
  }
  return Math.max(MIN_TIMEOUT_S, Math.min(seconds, MAX_TIMEOUT_S));
}

export class PhoneBusy extends Error {}

/** One script on the phone at a time, across sessions and subagents in this Gateway. */
export function createPhoneLock() {
  let last: Promise<void> = Promise.resolve();
  return async function withPhone<T>(waitMs: number, work: () => Promise<T>): Promise<T> {
    const previous = last;
    let release = () => {};
    last = new Promise<void>((resolve) => {
      release = resolve;
    });
    let timer: NodeJS.Timeout | undefined;
    const ourTurn = await Promise.race([
      previous.then(() => true),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), waitMs);
      }),
    ]);
    clearTimeout(timer);
    if (!ourTurn) {
      void previous.then(release); // keep the queue moving for whoever is behind us
      throw new PhoneBusy();
    }
    try {
      return await work();
    } finally {
      release();
    }
  };
}

// Absolute PNG / JPEG / WebP paths. A relative path, or a suffix of one, does not count.
const IMAGE_PATH = /(?<![\w.])((?:[A-Za-z]:[\\/]|\/)[^\s"']+?\.(?:png|jpe?g|webp))(?![\w.])/gi;

/**
 * Last absolute image path in stdout that exists and was modified during this call.
 * A one-second slack covers filesystems that store timestamps in whole seconds.
 */
export function findScreenshot(stdout: string, sinceMs: number): string | undefined {
  const paths = [...stdout.matchAll(IMAGE_PATH)].map((match) => match[1]).reverse();
  return paths.find((candidate) => {
    try {
      const stat = statSync(candidate);
      return stat.isFile() && stat.mtimeMs >= sinceMs - 1000;
    } catch {
      return false;
    }
  });
}

type CommandResult = {
  stdout: string;
  stderr: string;
  code: number | null;
  termination?: string;
};

export type PhoneRuntime = {
  run: (
    argv: string[],
    options: {
      input: string;
      cwd: string;
      baseEnv: NodeJS.ProcessEnv;
      timeoutMs: number;
      signal?: AbortSignal;
      killProcessTree: boolean;
      maxOutputBytes: { stdout: number; stderr: number };
    },
  ) => Promise<CommandResult>;
  resizeToJpeg: (params: { buffer: Buffer; maxSide: number; quality: number }) => Promise<Buffer>;
  imageSize: (buffer: Buffer) => Promise<{ width?: number; height?: number } | null>;
};

export type PhoneResult = {
  content: Array<
    { type: "text"; text: string } | { type: "image"; data: string; mimeType: string }
  >;
  details: Record<string, unknown>;
};

function result(fields: Record<string, unknown>, image?: PhoneResult["content"][number]): PhoneResult {
  const present = Object.fromEntries(
    Object.entries(fields).filter(([, value]) => value !== undefined && value !== ""),
  );
  // The model reads `content`. `details` stays small: OpenClaw keeps it with the session.
  const { output: _output, stderr: _stderr, ...details } = present;
  return {
    content: [{ type: "text", text: JSON.stringify(present) }, ...(image ? [image] : [])],
    details,
  };
}

export function refused(error: string): PhoneResult {
  return result({ ok: false, error });
}

/** A reduced image for the model, or undefined to keep the text result. */
async function attachedImage(file: string, runtime: PhoneRuntime) {
  try {
    const jpeg = await runtime.resizeToJpeg({
      buffer: await readFile(file),
      maxSide: IMAGE_MAX_SIDE,
      quality: IMAGE_QUALITY,
    });
    const size = await runtime.imageSize(jpeg);
    return {
      image: { type: "image" as const, data: jpeg.toString("base64"), mimeType: "image/jpeg" },
      size: size?.width && size.height ? [size.width, size.height] : undefined,
    };
  } catch {
    return undefined;
  }
}

/** The workspace folder, or its parent until the first script has created it. */
function scriptCwd(workspace: string): string {
  const parent = path.dirname(workspace);
  mkdirSync(parent, { recursive: true });
  try {
    return statSync(workspace).isDirectory() ? workspace : parent;
  } catch {
    return parent; // phone-harness seeds its default helpers into a folder that does not exist
  }
}

export async function runPhoneScript(params: {
  code: string;
  timeoutS: number;
  workspace: string;
  env: NodeJS.ProcessEnv;
  signal?: AbortSignal;
  runtime: PhoneRuntime;
}): Promise<PhoneResult> {
  const { code, timeoutS, workspace, env, signal, runtime } = params;
  const startedMs = Date.now();
  let command: CommandResult;
  try {
    command = await runtime.run(phoneCommand(), {
      input: code,
      cwd: scriptCwd(workspace),
      baseEnv: childEnv(env, workspace),
      timeoutMs: timeoutS * 1000,
      signal,
      killProcessTree: true,
      maxOutputBytes: { stdout: OUTPUT_CAP, stderr: STDERR_CAP },
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return refused(
        "phone_exec runs phone-harness with uv, and uvx is not on the Gateway's PATH. " +
          "Install uv: https://docs.astral.sh/uv/getting-started/installation/",
      );
    }
    return refused(`Failed to launch phone-harness: ${String(error)}`);
  }

  const output = command.stdout;
  const stderr = command.stderr.trim();
  if (command.termination === "timeout") {
    return result({
      ok: false,
      timedOut: true,
      timeoutMs: timeoutS * 1000,
      error:
        `phone_exec timed out after ${timeoutS}s. Split the work into smaller scripts ` +
        `or raise timeout_s (max ${MAX_TIMEOUT_S}).`,
      output,
      stderr,
    });
  }
  const ok = command.code === 0;
  const shot = findScreenshot(output, startedMs);
  const attached = shot ? await attachedImage(shot, runtime) : undefined;
  return result(
    {
      ok,
      exitCode: command.code ?? undefined,
      error: command.termination === "signal" ? "phone_exec was cancelled." : undefined,
      output,
      stderr,
      screenshots: shot ? [shot] : undefined,
      attachedImageSize: attached?.size,
      workspace,
    },
    attached?.image,
  );
}
