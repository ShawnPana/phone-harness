/**
 * Phone Harness for OpenClaw: one `phone_exec` tool.
 *
 * The model writes a Python script, the approval gate asks the user, and the plugin runs the
 * script on the phone. A native plugin runs inside the Gateway, so the script runs on the Gateway
 * host, outside any agent sandbox. See phone-exec.ts and approval.ts.
 */
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "typebox";
import {
  classifierAutoRun,
  classifyWithPython,
  createApprovalGate,
  HOOK_TIMEOUT_MS,
  OWNER_ONLY,
  TOOL_NAME,
} from "./approval.ts";
import {
  DEFAULT_TIMEOUT_S,
  executionBudgetMs,
  failure,
  MAX_TIMEOUT_S,
  phoneExec,
  type Env,
  type PhoneExecParams,
} from "./phone-exec.ts";

const DESCRIPTION =
  "Drive the user's real phone with Phone Harness: their iPhone through the Mac's iPhone Mirroring " +
  "window, an Android over adb, or a Phone Harness Cloud phone. `code` runs as Python with the phone " +
  "helpers pre-imported; print() whatever you need back. Each call is a fresh interpreter (variables " +
  "do not persist); the phone keeps its state. Start with ensure_device(); it covers iPhone " +
  "Mirroring, Android, and a cloud phone. Read what is on screen with screen(). screenshot() prints " +
  "the image path it saved, and that image is attached to this result. Ask the user before anything " +
  "outward-facing or hard to undo (sending, posting, buying, deleting, changing settings). Never " +
  "type passwords, PINs or 2FA codes. Read the phone-harness skill for the helpers.";

const PARAMETERS = Type.Object(
  {
    code: Type.String({
      description: "Python using the pre-imported phone-harness helpers. print() what you need back.",
    }),
    timeout_s: Type.Optional(
      Type.Integer({
        default: DEFAULT_TIMEOUT_S,
        description: `Max seconds for the script (default ${DEFAULT_TIMEOUT_S}, max ${MAX_TIMEOUT_S}).`,
      }),
    ),
  },
  { additionalProperties: false },
);

/** Where notes live: an inherited PH_AGENT_WORKSPACE, else one folder in OpenClaw's state directory. */
export function workspacePath(env: Env, stateDir: string): string {
  const inherited = env.PH_AGENT_WORKSPACE?.trim();
  if (!inherited) return join(stateDir, "phone-harness", "agent-workspace");
  return inherited === "~" || inherited.startsWith("~/") ? join(homedir(), inherited.slice(1)) : inherited;
}

export default definePluginEntry({
  id: "phone-harness",
  name: "Phone Harness",
  description: "Drive a real phone from OpenClaw with one phone_exec tool.",
  register(api) {
    const env = process.env;
    const classifier = fileURLToPath(new URL("../classifier.py", import.meta.url));

    api.registerTool(
      (context) => {
        // The script runs on the Gateway host, so a sandboxed session must not get the tool.
        if (context.sandboxed) return null;
        return {
          name: TOOL_NAME,
          label: "Phone",
          description: DESCRIPTION,
          parameters: PARAMETERS,
          executionMode: "sequential",
          // Results carry screenshots, which the hidden catalog bridges cannot return.
          catalogMode: "direct-only",
          getExecutionTimeoutMs: (params) => executionBudgetMs(params as PhoneExecParams),
          // The gate refuses non-owners too, but only where the hook can see the requester. Refusing
          // here and not hiding the tool tells the model why, so it does not go looking for another
          // way onto the phone: with the tool hidden, a test run fell back to the shell.
          execute: async (_toolCallId, params, signal) =>
            context.senderIsOwner === false
              ? failure(OWNER_ONLY)
              : phoneExec(params as PhoneExecParams, {
                  env,
                  workspace: workspacePath(env, api.runtime.state.resolveStateDir(env)),
                  signal,
                }),
        };
      },
      { name: TOOL_NAME },
    );

    api.on(
      "before_tool_call",
      createApprovalGate({
        autoRun: () => classifierAutoRun(env, api.pluginConfig?.classifierAutoRun),
        classify: classifyWithPython(classifier, env),
      }),
      { timeoutMs: HOOK_TIMEOUT_MS },
    );
  },
});
