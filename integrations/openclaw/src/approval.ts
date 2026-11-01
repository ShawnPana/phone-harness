/**
 * The approval gate in front of `phone_exec`.
 *
 * - Every call asks the user first, through OpenClaw's plugin approvals. Nobody to answer means
 *   the script does not run.
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
const DESCRIPTION_LIMIT = 512;
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
 * The script as the approver reads it, cut to fit the Gateway's description limit. A long script
 * keeps both ends, so padding cannot push what it really does out of the approver's sight.
 */
export function scriptSummary(code: string, prefix = "phone_exec: "): string {
  const body = code.split(/\s+/).filter(Boolean).join(" ") || "(empty)";
  const room = DESCRIPTION_LIMIT - prefix.length;
  if (body.length <= room) return prefix + body;
  const gap = (hidden: number) => ` ...[${hidden} characters hidden]... `;
  const keep = room - gap(body.length).length;
  const tail = Math.floor(keep / 2);
  return prefix + body.slice(0, keep - tail) + gap(body.length - keep) + body.slice(-tail);
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
    let description = scriptSummary(code);
    if (options.autoRun()) {
      let reason: string | null;
      try {
        reason = await options.classify(code);
      } catch {
        reason = "the classifier could not check";
      }
      if (reason === null) return undefined;
      description = scriptSummary(
        code,
        `This script ${reason}, which reaches past the phone onto this computer. Classifier auto-run ` +
          "is a heuristic and can be bypassed. phone_exec: ",
      );
    }
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
