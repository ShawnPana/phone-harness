/**
 * The environment a phone script runs in.
 *
 * Native plugins live in the Gateway process, so its environment holds provider keys and
 * channel tokens. A script is model-written Python: it gets none of them, and telemetry stays
 * off unless the user opted in. Same rules as integrations/hermes/__init__.py.
 */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

// OpenClaw's own names for credentials: SECRET_ENV_NAME_RE, plus the blocked patterns its
// sandbox adds. Neither is exported by the plugin SDK, and this covers Hermes's
// *_API_KEY / *_TOKEN / *_SECRET. PATH, HOME, ANDROID_* and the rest stay, so adb, iPhone
// Mirroring and a cloud phone still find what they need.
const SECRET_NAME =
  /_?(API_KEY|TOKEN|PASSWORD|PRIVATE_KEY|SECRET)$|_ADMIN_KEY$|^AWS_(SECRET_ACCESS_KEY|SECRET_KEY)$/i;

// Same enabling spellings as phone_harness.config's bool env override.
const TRUTHY = new Set(["1", "true", "yes", "on"]);

function truthy(value: unknown): boolean {
  if (typeof value === "string") {
    return TRUTHY.has(value.trim().toLowerCase());
  }
  return value === true || (typeof value === "number" && value !== 0);
}

/** config.json, using the same directory rules as phone_harness.config.config_dir. */
export function configJsonPath(env: NodeJS.ProcessEnv): string {
  if (env.PHONE_HARNESS_HOME) {
    return path.join(env.PHONE_HARNESS_HOME, "config", "config.json");
  }
  const base = env.XDG_CONFIG_HOME || path.join(homedir(), ".config");
  return path.join(base, "phone-harness", "config.json");
}

/**
 * True only for an explicit opt-in.
 *
 * Opt-in is `phone-harness config set telemetry true` (a value stored in config.json) or
 * PHONE_HARNESS_TELEMETRY set to an enabling value. The built-in default is on for the CLI
 * and is not an opt-in. An explicit false stays off.
 */
export function telemetryOptedIn(env: NodeJS.ProcessEnv): boolean {
  const raw = env.PHONE_HARNESS_TELEMETRY;
  if (raw !== undefined) {
    return truthy(raw);
  }
  try {
    const data: unknown = JSON.parse(readFileSync(configJsonPath(env), "utf8"));
    if (data === null || typeof data !== "object" || !("telemetry" in data)) {
      return false;
    }
    return truthy(data.telemetry);
  } catch {
    return false; // no config file, or one we cannot read: not an opt-in
  }
}

export function scrubSecrets(env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return Object.fromEntries(Object.entries(env).filter(([key]) => !SECRET_NAME.test(key)));
}

/**
 * The Gateway's environment minus credentials, plus any PHONE_HARNESS_* the user exported,
 * the workspace, and the client name. It does not choose a platform.
 */
export function childEnv(parent: NodeJS.ProcessEnv, workspace: string): NodeJS.ProcessEnv {
  const exported = Object.entries(parent).filter(([key]) => key.startsWith("PHONE_HARNESS_"));
  const env: NodeJS.ProcessEnv = {
    ...scrubSecrets(parent),
    ...Object.fromEntries(exported),
    PH_AGENT_WORKSPACE: workspace,
    PH_CLIENT: "openclaw",
  };
  if (!telemetryOptedIn(parent)) {
    env.PHONE_HARNESS_TELEMETRY = "0";
  }
  return env;
}
