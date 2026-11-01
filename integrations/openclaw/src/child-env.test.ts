import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { childEnv, configJsonPath, scrubSecrets, telemetryOptedIn } from "./child-env.js";

describe("secrets", () => {
  it("removes credentials and keeps what the phone needs", () => {
    const kept = scrubSecrets({
      OPENAI_API_KEY: "x",
      DISCORD_BOT_TOKEN: "x",
      OPENCLAW_GATEWAY_TOKEN: "x",
      OPENCLAW_GATEWAY_PASSWORD: "x",
      WEBHOOK_SECRET: "x",
      NOSTR_PRIVATE_KEY: "x",
      AWS_SECRET_ACCESS_KEY: "x",
      SOME_ADMIN_KEY: "x",
      lower_api_key: "x",
      PATH: "/bin",
      HOME: "/home/me",
      ANDROID_HOME: "/sdk",
      XDG_CONFIG_HOME: "/cfg",
    });
    expect(kept).toEqual({
      PATH: "/bin",
      HOME: "/home/me",
      ANDROID_HOME: "/sdk",
      XDG_CONFIG_HOME: "/cfg",
    });
  });

  it("puts back every PHONE_HARNESS_ variable, a cloud key included", () => {
    const env = childEnv(
      { PHONE_HARNESS_API_KEY: "cloud", PHONE_HARNESS_PLATFORM: "android", OTHER_API_KEY: "x" },
      "/ws",
    );
    expect(env.PHONE_HARNESS_API_KEY).toBe("cloud");
    expect(env.PHONE_HARNESS_PLATFORM).toBe("android");
    expect(env.OTHER_API_KEY).toBeUndefined();
  });

  it("sets the workspace and the client and does not invent a platform", () => {
    const env = childEnv({ PATH: "/bin" }, "/ws");
    expect(env).toMatchObject({ PATH: "/bin", PH_AGENT_WORKSPACE: "/ws", PH_CLIENT: "openclaw" });
    expect(env.PHONE_HARNESS_PLATFORM).toBeUndefined();
  });
});

describe("telemetry", () => {
  let home: string;
  const write = (value: unknown) => {
    mkdirSync(path.join(home, "config"), { recursive: true });
    writeFileSync(path.join(home, "config", "config.json"), JSON.stringify(value));
  };
  const envWith = (extra: NodeJS.ProcessEnv = {}) => ({ PHONE_HARNESS_HOME: home, ...extra });

  beforeEach(() => {
    home = mkdtempSync(path.join(tmpdir(), "ph-openclaw-"));
  });
  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  it("is off when the user never touched the setting", () => {
    expect(telemetryOptedIn(envWith())).toBe(false);
    expect(childEnv(envWith(), "/ws").PHONE_HARNESS_TELEMETRY).toBe("0");
  });

  it("is off when config.json has no telemetry key", () => {
    write({ platform: "ios" });
    expect(telemetryOptedIn(envWith())).toBe(false);
  });

  it("is on for an explicit stored true, and the plugin leaves the variable alone", () => {
    write({ telemetry: true });
    expect(telemetryOptedIn(envWith())).toBe(true);
    expect(childEnv(envWith(), "/ws").PHONE_HARNESS_TELEMETRY).toBeUndefined();
  });

  it("stays off for an explicit stored false", () => {
    write({ telemetry: false });
    expect(childEnv(envWith(), "/ws").PHONE_HARNESS_TELEMETRY).toBe("0");
  });

  it("stays off when config.json cannot be parsed", () => {
    mkdirSync(path.join(home, "config"), { recursive: true });
    writeFileSync(path.join(home, "config", "config.json"), "{not json");
    expect(telemetryOptedIn(envWith())).toBe(false);
  });

  it("lets the environment variable win in both directions", () => {
    write({ telemetry: true });
    expect(telemetryOptedIn(envWith({ PHONE_HARNESS_TELEMETRY: "0" }))).toBe(false);
    expect(childEnv(envWith({ PHONE_HARNESS_TELEMETRY: "0" }), "/ws").PHONE_HARNESS_TELEMETRY).toBe("0");
    write({ telemetry: false });
    for (const on of ["1", "true", "YES", " on "]) {
      expect(telemetryOptedIn(envWith({ PHONE_HARNESS_TELEMETRY: on }))).toBe(true);
    }
  });

  it("treats an empty variable as off, as the CLI does", () => {
    expect(telemetryOptedIn(envWith({ PHONE_HARNESS_TELEMETRY: "" }))).toBe(false);
  });

  it("reads the same config file as phone_harness.config", () => {
    expect(configJsonPath({ PHONE_HARNESS_HOME: "/h" })).toBe(path.join("/h", "config", "config.json"));
    expect(configJsonPath({ XDG_CONFIG_HOME: "/x" })).toBe(
      path.join("/x", "phone-harness", "config.json"),
    );
    expect(configJsonPath({})).toBe(path.join(homedir(), ".config", "phone-harness", "config.json"));
  });
});
