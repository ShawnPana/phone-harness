/**
 * The approval gate in front of `phone_exec`.
 *
 * - Every call asks the user first, through OpenClaw's plugin approvals. Nobody to answer means
 *   the script does not run.
 * - The prompt shows the whole script. One too long for the prompt is refused, never cut, so no
 *   one approves code they could not read.
 * - A sender OpenClaw knows is not the owner is refused. Chats can answer their own approvals,
 *   so the prompt alone would let them drive the owner's phone.
 * - "Allow always" covers later calls in the same session. OpenClaw leaves that memory to the
 *   plugin. It lives in this process, so a Gateway restart or a new session asks again.
 * - Classifier-only auto-run is an explicit opt-in. That check is a heuristic and can be bypassed.
 */
import { spawn } from "node:child_process";
import { childEnv, truthy, type Env } from "./phone-exec.ts";

export const TOOL_NAME = "phone_exec";
export const CLASSIFIER_AUTO_RUN_ENV = "PHONE_HARNESS_CLASSIFIER_AUTO_RUN";
export const OWNER_ONLY = "Only the owner of this OpenClaw can drive the phone.";

const APPROVAL_TIMEOUT_MS = 300_000;
/** OpenClaw's cap on a plugin approval's description (PLUGIN_APPROVAL_DESCRIPTION_MAX_LENGTH). */
export const DESCRIPTION_LIMIT = 512;
const PREFIX = "phone_exec: ";
/** The longest script, as the prompt shows it, that fits a prompt whole. */
export const MAX_SHOWN_CHARS = DESCRIPTION_LIMIT - PREFIX.length;
const CLASSIFIER_TIMEOUT_MS = 20_000;
/** Above CLASSIFIER_TIMEOUT_MS, so a slow classifier ends in a prompt and not a blocked call. */
export const HOOK_TIMEOUT_MS = 30_000;

type Decision = "allow-once" | "allow-always" | "deny" | "timeout" | "cancelled";

type GateResult =
  | { block: true; blockReason: string }
  | {
      requireApproval: {
        title: string;
        description: string;
        severity: "warning";
        timeoutMs: number;
        onResolution: (decision: Decision) => void;
      };
    }
  | undefined;

type GateEvent = { toolName: string; params: Record<string, unknown> };
type GateContext = {
  sessionKey?: string;
  sessionId?: string;
  requester?: { senderIsOwner?: boolean };
};

/** Why a script reaches past the phone, or null when it only drives the phone. */
export type Classify = (code: string) => Promise<string | null>;

/**
 * PHONE_HARNESS_CLASSIFIER_AUTO_RUN, when set, wins over the plugin setting so one process can
 * force the gate on or off. The default is off.
 */
export function classifierAutoRun(env: Env, setting: unknown): boolean {
  const raw = env[CLASSIFIER_AUTO_RUN_ENV];
  return raw !== undefined ? truthy(raw) : truthy(setting ?? false);
}

/**
 * The whole script as the approver reads it. Runs of spaces, tabs and newlines show as one space,
 * and control and invisible formatting characters are spelled out, so nothing that runs is hidden.
 */
export function shownScript(code: string): string {
  const body = code.split(/[ \t\n\r\f\v]+/).filter(Boolean).join(" ");
  const spelled = body.replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, (char) => `\\u{${(char.codePointAt(0) ?? 0).toString(16)}}`);
  return spelled || "(empty)";
}

/** The prompt with the whole script, or null when it does not fit. A script is never cut. */
export function approvalDescription(code: string, prefix = PREFIX): string | null {
  const description = prefix + shownScript(code);
  return description.length <= DESCRIPTION_LIMIT ? description : null;
}

/** What the model hears when a script is too long for anyone to read it in the prompt. */
export function tooLongToShow(code: string, prefix = PREFIX): string {
  return (
    `This script is ${shownScript(code).length} characters as the approval prompt shows it, and the ` +
    `prompt holds ${DESCRIPTION_LIMIT - prefix.length}. phone_exec only runs a script the user could ` +
    "read in full, so it did not run. Split the work into shorter phone_exec calls; the phone keeps " +
    "its state between them."
  );
}

export function createApprovalGate(options: { autoRun: () => boolean; classify: Classify }) {
  const trustedSessions = new Set<string>();

  return async function beforeToolCall(event: GateEvent, ctx: GateContext): Promise<GateResult> {
    if (event.toolName !== TOOL_NAME) return undefined;
    if (ctx.requester?.senderIsOwner === false) return { block: true, blockReason: OWNER_ONLY };
    // sessionId changes on a new conversation in the same chat; sessionKey does not.
    const session = ctx.sessionId ?? ctx.sessionKey;
    if (session !== undefined && trustedSessions.has(session)) return undefined;

    const code = typeof event.params.code === "string" ? event.params.code : "";
    let prefix = PREFIX;
    if (options.autoRun()) {
      let reason: string | null;
      try {
        reason = await options.classify(code);
      } catch {
        reason = "the classifier could not check";
      }
      if (reason === null) return undefined;
      prefix =
        `This script ${reason}, which reaches past the phone onto this computer. Classifier auto-run ` +
        `is a heuristic and can be bypassed. ${PREFIX}`;
    }
    const description = approvalDescription(code, prefix);
    if (description === null) return { block: true, blockReason: tooLongToShow(code, prefix) };
    return {
      requireApproval: {
        title: "Run a script on the phone",
        description,
        severity: "warning",
        timeoutMs: APPROVAL_TIMEOUT_MS,
        onResolution(decision) {
          if (decision === "allow-always" && session !== undefined) trustedSessions.add(session);
        },
      },
    };
  };
}

/** Ask classifier.py about a script. It parses the script and never runs it. Rejects on any failure. */
export function classifyWithPython(script: string, env: Env): Classify {
  return (code) =>
    new Promise((resolve, reject) => {
      const child = spawn(
        "uv",
        ["run", "--quiet", "--no-project", "--python", "3.12", "python", "-I", script],
        {
          env: childEnv(env, "") as NodeJS.ProcessEnv,
          stdio: ["pipe", "pipe", "ignore"],
          timeout: CLASSIFIER_TIMEOUT_MS,
          killSignal: "SIGKILL",
        },
      );
      let out = "";
      child.stdout.setEncoding("utf8").on("data", (chunk: string) => {
        out += chunk;
      });
      child.stdin.on("error", () => {});
      child.on("error", reject);
      child.on("close", (exitCode) => {
        try {
          if (exitCode !== 0) throw new Error(`classifier exited ${exitCode}`);
          const { reason } = JSON.parse(out) as { reason: unknown };
          if (reason !== null && typeof reason !== "string") throw new Error("bad classifier output");
          resolve(reason);
        } catch (error) {
          reject(error);
        }
      });
      child.stdin.end(code);
    });
}
