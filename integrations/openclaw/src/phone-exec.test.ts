import { mkdtempSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_TIMEOUT_S,
  DESCRIPTION_MAX,
  MAX_TIMEOUT_S,
  MIN_TIMEOUT_S,
  PhoneBusy,
  type PhoneRuntime,
  clampTimeoutS,
  createPhoneLock,
  describeScript,
  findScreenshot,
  phoneCommand,
  runPhoneScript,
} from "./phone-exec.js";
import { PHONE_HARNESS_VERSION } from "./pin.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "ph-openclaw-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function fakeRuntime(result: Record<string, unknown>): PhoneRuntime {
  return {
    run: vi.fn(async () => ({ stdout: "", stderr: "", code: 0, termination: "exit", ...result })),
    resizeToJpeg: vi.fn(async () => Buffer.from("jpeg")),
    imageSize: vi.fn(async () => ({ width: 554, height: 1200 })),
  };
}

function run(runtime: PhoneRuntime, code = "print(1)", timeoutS = 30) {
  return runPhoneScript({
    code,
    timeoutS,
    workspace: path.join(dir, "agent-workspace"),
    env: { PATH: "/bin", OPENAI_API_KEY: "secret" },
    runtime,
  });
}

function text(result: Awaited<ReturnType<typeof run>>) {
  return JSON.parse((result.content[0] as { text: string }).text);
}

describe("the command", () => {
  it("runs the pinned release through uvx", () => {
    expect(PHONE_HARNESS_VERSION).toMatch(/^\d+\.\d+\.\d+$/);
    const argv = phoneCommand();
    expect(argv[0]).toBe("uvx");
    expect(argv).toContain(`phone-harness==${PHONE_HARNESS_VERSION}`);
    expect(argv.at(-1)).toBe("phone-harness");
  });

  it("clamps the timeout", () => {
    expect(clampTimeoutS(undefined)).toBe(DEFAULT_TIMEOUT_S);
    expect(clampTimeoutS("nonsense")).toBe(DEFAULT_TIMEOUT_S);
    expect(clampTimeoutS(1)).toBe(MIN_TIMEOUT_S);
    expect(clampTimeoutS(99_999)).toBe(MAX_TIMEOUT_S);
    expect(clampTimeoutS(60)).toBe(60);
  });
});

describe("the approval text", () => {
  it("shows a short script whole, on one line", () => {
    const shown = describeScript('ensure_device()\n\n   print(screen())\n');
    expect(shown).toContain("ensure_device() print(screen())");
    expect(shown.startsWith("Runs as Python on this computer")).toBe(true);
  });

  it("fits the Gateway's cap and says how much it left out", () => {
    const shown = describeScript("x = 1\n".repeat(400));
    expect(shown.length).toBeLessThanOrEqual(DESCRIPTION_MAX);
    expect(shown).toMatch(/\[\+\d+ chars hidden\]/);
  });

  it("cannot hide what a long script ends with", () => {
    const code = `${"# harmless\n".repeat(200)}import os; os.remove("/important")`;
    const shown = describeScript(code);
    expect(shown.length).toBeLessThanOrEqual(DESCRIPTION_MAX);
    expect(shown).toContain('os.remove("/important")');
  });

  it("is not fooled by padding", () => {
    const shown = describeScript(`print(1)${" ".repeat(10_000)}\n${"\t".repeat(10_000)}steal()`);
    expect(shown).toContain("print(1) steal()");
  });

  it("fits at the exact boundary", () => {
    for (let size = 440; size < 480; size += 1) {
      expect(describeScript("y".repeat(size)).length).toBeLessThanOrEqual(DESCRIPTION_MAX);
    }
  });
});

describe("one script at a time", () => {
  it("runs scripts in the order they arrived", async () => {
    const withPhone = createPhoneLock();
    const seen: string[] = [];
    let finishFirst = () => {};
    const first = withPhone(1000, async () => {
      seen.push("first starts");
      await new Promise<void>((resolve) => {
        finishFirst = resolve;
      });
      seen.push("first ends");
    });
    const second = withPhone(1000, async () => {
      seen.push("second");
    });
    await vi.waitFor(() => expect(seen).toEqual(["first starts"]));
    finishFirst();
    await Promise.all([first, second]);
    expect(seen).toEqual(["first starts", "first ends", "second"]);
  });

  it("reports busy after the wait and keeps the queue moving", async () => {
    const withPhone = createPhoneLock();
    let finishFirst = () => {};
    const first = withPhone(1000, () => new Promise<void>((resolve) => (finishFirst = resolve)));
    await expect(withPhone(10, async () => "no")).rejects.toBeInstanceOf(PhoneBusy);
    const third = withPhone(1000, async () => "third ran");
    finishFirst();
    await first;
    await expect(third).resolves.toBe("third ran");
  });

  it("lets the next script run after one throws", async () => {
    const withPhone = createPhoneLock();
    await expect(withPhone(10, () => Promise.reject(new Error("boom")))).rejects.toThrow("boom");
    await expect(withPhone(10, async () => "next")).resolves.toBe("next");
  });
});

describe("finding the screenshot", () => {
  it("keeps the last image written during the call", () => {
    const first = path.join(dir, "first.png");
    const last = path.join(dir, "last.jpg");
    writeFileSync(first, "x");
    writeFileSync(last, "x");
    expect(findScreenshot(`${first}\nnoise\n${last}\n`, Date.now())).toBe(last);
  });

  it("ignores an older image, a relative path and a missing file", () => {
    const old = path.join(dir, "old.png");
    writeFileSync(old, "x");
    const past = new Date(Date.now() - 60_000);
    utimesSync(old, past, past);
    const stdout = `${old}\nshots/rel.png\n${path.join(dir, "gone.png")}\n`;
    expect(findScreenshot(stdout, Date.now())).toBeUndefined();
  });

  it("allows one second of timestamp slack", () => {
    const shot = path.join(dir, "shot.webp");
    writeFileSync(shot, "x");
    const nearly = new Date(Date.now() - 900);
    utimesSync(shot, nearly, nearly);
    expect(findScreenshot(shot, Date.now())).toBe(shot);
  });
});

describe("running a script", () => {
  it("sends the script on stdin with a scrubbed environment and a process-tree kill", async () => {
    const runtime = fakeRuntime({ stdout: "hello\n" });
    const result = await run(runtime, "print('hello')");
    const [argv, options] = vi.mocked(runtime.run).mock.calls[0];
    expect(argv).toEqual(phoneCommand());
    expect(options.input).toBe("print('hello')");
    expect(options.killProcessTree).toBe(true);
    expect(options.timeoutMs).toBe(30_000);
    expect(options.baseEnv.OPENAI_API_KEY).toBeUndefined();
    expect(options.baseEnv.PH_CLIENT).toBe("openclaw");
    expect(text(result)).toMatchObject({ ok: true, exitCode: 0, output: "hello\n" });
  });

  it("leaves a missing workspace for phone-harness to seed", async () => {
    const runtime = fakeRuntime({});
    await run(runtime);
    const [, options] = vi.mocked(runtime.run).mock.calls[0];
    expect(options.cwd).toBe(dir);
    expect(options.baseEnv.PH_AGENT_WORKSPACE).toBe(path.join(dir, "agent-workspace"));
  });

  it("keeps the model's text out of details, which stay with the session", async () => {
    const result = await run(fakeRuntime({ stdout: "secret screen", stderr: "warn" }));
    expect(result.details).toMatchObject({ ok: true, exitCode: 0 });
    expect(JSON.stringify(result.details)).not.toContain("secret screen");
  });

  it("reports a failed script with its exit code and stderr", async () => {
    const result = await run(fakeRuntime({ code: 1, stderr: "Traceback\n" }));
    expect(text(result)).toMatchObject({ ok: false, exitCode: 1, stderr: "Traceback" });
    expect(result.details).toMatchObject({ ok: false, exitCode: 1 });
  });

  it("reports a timeout the way OpenClaw's fallback warning reads it", async () => {
    const result = await run(fakeRuntime({ code: 124, termination: "timeout", stdout: "partial" }), "x", 5);
    expect(result.details).toMatchObject({ ok: false, timedOut: true, timeoutMs: 5000 });
    expect(text(result).output).toBe("partial");
  });

  it("reports a cancelled run", async () => {
    const result = await run(fakeRuntime({ code: null, termination: "signal" }));
    expect(text(result)).toMatchObject({ ok: false, error: "phone_exec was cancelled." });
  });

  it("says how to get uv when it is missing", async () => {
    const runtime = fakeRuntime({});
    vi.mocked(runtime.run).mockRejectedValue(Object.assign(new Error("nope"), { code: "ENOENT" }));
    const body = text(await run(runtime));
    expect(body.ok).toBe(false);
    expect(body.error).toContain("https://docs.astral.sh/uv/");
  });

  it("does not throw when the launch fails some other way", async () => {
    const runtime = fakeRuntime({});
    vi.mocked(runtime.run).mockRejectedValue(new Error("EACCES"));
    expect(text(await run(runtime)).error).toContain("EACCES");
  });
});

describe("attaching a screenshot", () => {
  it("attaches a reduced JPEG and says its size", async () => {
    const shot = path.join(dir, "shot.png");
    writeFileSync(shot, "png");
    const runtime = fakeRuntime({ stdout: `${shot}\n` });
    const result = await run(runtime);
    expect(result.content[1]).toEqual({
      type: "image",
      data: Buffer.from("jpeg").toString("base64"),
      mimeType: "image/jpeg",
    });
    expect(text(result)).toMatchObject({ screenshots: [shot], attachedImageSize: [554, 1200] });
  });

  it("still attaches what a failed script saved", async () => {
    const shot = path.join(dir, "shot.png");
    writeFileSync(shot, "png");
    const result = await run(fakeRuntime({ stdout: `${shot}\n`, code: 1 }));
    expect(result.content).toHaveLength(2);
    expect(result.details).toMatchObject({ ok: false });
  });

  it("falls back to text, with the path, when reducing the image fails", async () => {
    const shot = path.join(dir, "shot.png");
    writeFileSync(shot, "png");
    const runtime = fakeRuntime({ stdout: `${shot}\n` });
    vi.mocked(runtime.resizeToJpeg).mockRejectedValue(new Error("no image processor"));
    const result = await run(runtime);
    expect(result.content).toHaveLength(1);
    expect(text(result).screenshots).toEqual([shot]);
  });

  it("attaches nothing when the script saved nothing", async () => {
    const runtime = fakeRuntime({ stdout: "no image here\n" });
    const result = await run(runtime);
    expect(result.content).toHaveLength(1);
    expect(runtime.resizeToJpeg).not.toHaveBeenCalled();
  });
});
