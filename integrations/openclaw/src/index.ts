/**
 * phone-harness for OpenClaw: one `phone_exec` tool.
 *
 * The model writes a Python script. The plugin pipes it on stdin to the pinned phone-harness
 * (see pin.ts), which runs it with the phone helpers pre-imported against the user's phone and
 * hands stdout back as the tool result. Each call is a fresh interpreter; the phone and the
 * agent workspace persist.
 *
 * Safety, in order of what it protects:
 * - The tool is optional: the model cannot see it until the user allowlists it.
 * - Every call asks first, through a `before_tool_call` hook. There is no allow-always: a script
 *   is arbitrary code on this computer, so no answer covers a later one.
 * - `execute` runs only the exact script that was approved for that exact tool call. OpenClaw
 *   skips approval entirely when the hook is not registered in its active runner, so without
 *   this check a load path that missed the hook would run scripts unasked.
 * - Sessions in a sandbox do not get the tool. It would run on the host, outside the sandbox.
 * - The child environment has no credentials and telemetry off unless the user opted in.
 * - One script drives the phone at a time, across sessions and subagents in this Gateway.
 */
import { homedir } from "node:os";
import path from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { Type } from "typebox";
import {
  DEFAULT_TIMEOUT_S,
  LOCK_WAIT_MS,
  MAX_TIMEOUT_S,
  PhoneBusy,
  type PhoneRuntime,
  clampTimeoutS,
  createPhoneLock,
  describeScript,
  refused,
  runPhoneScript,
} from "./phone-exec.js";

const PLUGIN_ID = "phone-harness";
const TOOL = "phone_exec";

const DESCRIPTION =
  "Drive the user's real phone with phone-harness: their iPhone through the Mac's iPhone " +
  "Mirroring window, an Android over adb, or a Phone Harness Cloud phone. `code` runs as Python " +
  "on the Gateway host with the phone helpers pre-imported; print() whatever you need back. " +
  "Each call is a fresh interpreter (variables do not persist); the phone keeps its state. The " +
  "user approves every call, so do one whole step in each script. Start with ensure_device(). " +
  "Read what is on screen with screen(). screenshot() prints the image path it saved, and that " +
  "image is attached to this result. Ask the user before anything outward-facing or hard to " +
  "undo (sending, posting, buying, deleting, changing settings). Never type passwords, PINs or " +
  "2FA codes. Read the phone-harness skill for the helpers. Do not run the phone-harness " +
  "command through a shell: that skips the approval this tool asks for.";

const PARAMETERS = Type.Object({
  code: Type.String({
    description: "Python using the pre-imported phone-harness helpers. print() what you need back.",
  }),
  timeout_s: Type.Optional(
    Type.Integer({
      description: `Max seconds for the script (default ${DEFAULT_TIMEOUT_S}, max ${MAX_TIMEOUT_S}).`,
    }),
  ),
});

type ScriptParams = { code?: unknown; timeout_s?: unknown };

function scriptOf(params: unknown): string {
  return String((params as ScriptParams | null)?.code ?? "");
}

// One per process, not per register(): OpenClaw's tool-discovery load mode registers tools
// apart from hooks, and a hook in one load has to be able to vouch for a tool from another.
// Keyed by tool call. The value is the script the user saw, so an approval covers that text
// and nothing else.
const approvedScripts = new Map<string, string>();
const withPhone = createPhoneLock();

export default definePluginEntry({
  id: PLUGIN_ID,
  name: "Phone Harness",
  description: "Let your agent drive a real phone. Every script asks you first.",
  register(api) {
    const runtime: PhoneRuntime = {
      run: (argv, options) => api.runtime.system.runCommandWithTimeout(argv, options),
      resizeToJpeg: (params) => api.runtime.media.resizeToJpeg(params),
      imageSize: (buffer) => api.runtime.media.getImageMetadata(buffer),
    };

    const workspace = () => {
      const inherited = process.env.PH_AGENT_WORKSPACE?.trim();
      if (inherited) {
        return inherited.replace(/^~(?=$|[\\/])/, homedir());
      }
      return path.join(api.runtime.state.resolveStateDir(process.env), PLUGIN_ID, "agent-workspace");
    };

    api.registerTool(
      (toolContext) => {
        if (toolContext.sandboxed) {
          return null;
        }
        return {
          name: TOOL,
          label: "Phone",
          description: DESCRIPTION,
          parameters: PARAMETERS,
          async execute(toolCallId: string, params: unknown, signal?: AbortSignal) {
            const code = scriptOf(params);
            if (!code.trim()) {
              return refused("No code provided. Start with: ensure_device(); print(screen_info())");
            }
            const approved = approvedScripts.get(toolCallId);
            approvedScripts.delete(toolCallId);
            if (approved !== code) {
              return refused("This script was not approved, so it did not run.");
            }
            try {
              return await withPhone(LOCK_WAIT_MS, () =>
                runPhoneScript({
                  code,
                  timeoutS: clampTimeoutS((params as ScriptParams).timeout_s),
                  workspace: workspace(),
                  env: process.env,
                  signal,
                  runtime,
                }),
              );
            } catch (error) {
              if (error instanceof PhoneBusy) {
                return refused(
                  `The phone is busy with another phone_exec script (waited ${LOCK_WAIT_MS / 1000}s). ` +
                    "Try again when it finishes.",
                );
              }
              throw error;
            }
          },
        };
      },
      { name: TOOL, optional: true },
    );

    api.on("before_tool_call", (event) => {
      if (event.toolName !== TOOL) {
        return;
      }
      const code = scriptOf(event.params);
      const { toolCallId } = event;
      if (!code.trim()) {
        return; // nothing runs; the tool answers with an error
      }
      return {
        requireApproval: {
          title: "Run a phone script",
          description: describeScript(code),
          severity: "warning",
          allowedDecisions: ["allow-once", "deny"],
          onResolution(decision) {
            if (decision === "allow-once" && toolCallId) {
              approvedScripts.set(toolCallId, code);
            }
          },
        },
      };
    });
  },
});
