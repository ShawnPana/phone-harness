import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  CLASSIFIER_AUTO_RUN_ENV,
  classifierAutoRun,
  classifyWithPython,
  approvalDescription,
  createApprovalGate,
  DESCRIPTION_LIMIT,
  MAX_SHOWN_CHARS,
  shownScript,
} from "./approval.ts";

const OWNER = { sessionId: "s1", sessionKey: "agent:main:main", requester: { senderIsOwner: true } };
const CALL = { toolName: "phone_exec", params: { code: "print(screen())" } };

function gate(options: Partial<Parameters<typeof createApprovalGate>[0]> = {}) {
  return createApprovalGate({
    autoRun: () => false,
    classify: () => {
      throw new Error("the classifier must not run unless the user opted in");
    },
    ...options,
  });
}

/** Answer the prompt the gate returned, the way OpenClaw does. */
function approval(result: Awaited<ReturnType<ReturnType<typeof gate>>>) {
  assert.ok(result && "requireApproval" in result, "expected a prompt");
  return result.requireApproval;
}

describe("the prompt", () => {
  it("asks on every call by default", async () => {
    const before = gate();
    approval(await before(CALL, OWNER));
    approval(await before(CALL, OWNER));
  });

  it("leaves other tools alone", async () => {
    assert.equal(await gate()({ toolName: "exec", params: {} }, OWNER), undefined);
  });

  it("asks when OpenClaw cannot say who is asking", async () => {
    approval(await gate()(CALL, {}));
  });

  it("shows the script and stays inside the Gateway's limits", async () => {
    const prompt = approval(await gate()({ ...CALL, params: { code: "tap_text(\n  'Send'\n)" } }, OWNER));
    assert.equal(prompt.description, "phone_exec: tap_text( 'Send' )");
    assert.ok(prompt.title.length <= 80);
    assert.ok(prompt.timeoutMs <= 600_000);
  });

  it("shows the whole of the longest script that fits", async () => {
    const code = `print(${"1".repeat(MAX_SHOWN_CHARS - 7)})`;
    assert.equal(code.length, MAX_SHOWN_CHARS);
    const prompt = approval(await gate()({ ...CALL, params: { code } }, OWNER));
    assert.equal(prompt.description, `phone_exec: ${code}`);
    assert.equal(prompt.description.length, DESCRIPTION_LIMIT);
  });

  it("refuses a script too long to show whole, and never shows part of one", async () => {
    // Padding in the middle used to drop out of a cut-down prompt. Now nothing is cut.
    const code = `ensure_device()\n# ${"x".repeat(5000)}\nopen('/etc/passwd')`;
    const result = await gate()({ ...CALL, params: { code } }, OWNER);
    assert.ok(result && "block" in result, "expected a refusal, not a prompt");
    assert.match(result.blockReason, new RegExp(`is ${shownScript(code).length} characters`));
    assert.match(result.blockReason, new RegExp(`holds ${MAX_SHOWN_CHARS}\\b`));
    assert.match(result.blockReason, /Split the work into shorter phone_exec calls/);
    assert.equal(approvalDescription(code), null);
  });

  it("counts a run of whitespace as one character", async () => {
    const code = `print(1)${" \n\t".repeat(10_000)}print(2)`;
    const prompt = approval(await gate()({ ...CALL, params: { code } }, OWNER));
    assert.equal(prompt.description, "phone_exec: print(1) print(2)");
  });

  it("spells out control and invisible characters instead of hiding them", async () => {
    // A right-to-left override, a zero-width space, an escape and a line separator.
    const code = "print('ok') #\u202e evil\u200b\u001b\u2028x";
    const prompt = approval(await gate()({ ...CALL, params: { code } }, OWNER));
    assert.equal(prompt.description, "phone_exec: print('ok') #\\u{202e} evil\\u{200b}\\u{1b}\\u{2028}x");
    assert.doesNotMatch(prompt.description, /[\u202e\u200b\u001b\u2028]/);
  });

  it("has something to show for an empty or malformed call", () => {
    assert.equal(shownScript(""), "(empty)");
    assert.equal(approvalDescription(""), "phone_exec: (empty)");
  });
});

describe("who may drive the phone", () => {
  const stranger = { sessionId: "s1", requester: { senderIsOwner: false } };

  it("refuses a sender OpenClaw knows is not the owner", async () => {
    const result = await gate()(CALL, stranger);
    assert.ok(result && "block" in result);
    assert.match(result.blockReason, /owner/);
  });

  it("refuses them even in a session the owner trusted", async () => {
    const before = gate();
    approval(await before(CALL, OWNER)).onResolution("allow-always");
    assert.equal(await before(CALL, OWNER), undefined);
    const result = await before(CALL, stranger);
    assert.ok(result && "block" in result);
  });

  it("refuses them even with classifier auto-run on", async () => {
    const result = await gate({ autoRun: () => true, classify: async () => null })(CALL, stranger);
    assert.ok(result && "block" in result);
  });
});

describe("allow-always", () => {
  it("covers later calls in the session", async () => {
    const before = gate();
    approval(await before(CALL, OWNER)).onResolution("allow-always");
    assert.equal(await before(CALL, OWNER), undefined);
  });

  it("does not reach a new conversation or another session", async () => {
    const before = gate();
    approval(await before(CALL, OWNER)).onResolution("allow-always");
    approval(await before(CALL, { ...OWNER, sessionId: "s2" }));
    approval(await before(CALL, { sessionKey: "agent:main:other" }));
  });

  it("does nothing without a session to remember", async () => {
    const before = gate();
    approval(await before(CALL, {})).onResolution("allow-always");
    approval(await before(CALL, {}));
  });

  for (const decision of ["allow-once", "deny", "timeout", "cancelled"] as const) {
    it(`is not implied by ${decision}`, async () => {
      const before = gate();
      approval(await before(CALL, OWNER)).onResolution(decision);
      approval(await before(CALL, OWNER));
    });
  }
});

describe("classifier auto-run", () => {
  it("is off unless the user opts in", () => {
    assert.equal(classifierAutoRun({}, undefined), false);
    assert.equal(classifierAutoRun({}, false), false);
    assert.equal(classifierAutoRun({}, true), true);
  });

  it("lets the variable win over the setting, both ways", () => {
    assert.equal(classifierAutoRun({ [CLASSIFIER_AUTO_RUN_ENV]: "0" }, true), false);
    assert.equal(classifierAutoRun({ [CLASSIFIER_AUTO_RUN_ENV]: "1" }, false), true);
  });

  it("runs a phone-only script without asking", async () => {
    const result = await gate({ autoRun: () => true, classify: async () => null })(CALL, OWNER);
    assert.equal(result, undefined);
  });

  it("still asks, and says why, for a script that reaches past the phone", async () => {
    const before = gate({ autoRun: () => true, classify: async () => "imports os" });
    const prompt = approval(await before(CALL, OWNER));
    assert.match(prompt.description, /imports os/);
    assert.match(prompt.description, /can be bypassed/);
    assert.ok(prompt.description.length <= 512);
  });

  it("refuses a flagged script too long to show beside the reason", async () => {
    const code = `import os\n${"print(1)\n".repeat(48)}`;
    assert.ok(shownScript(code).length < MAX_SHOWN_CHARS, "short enough for a plain prompt");
    const before = gate({ autoRun: () => true, classify: async () => "imports os" });
    const result = await before({ ...CALL, params: { code } }, OWNER);
    assert.ok(result && "block" in result);
    assert.match(result.blockReason, /Split the work/);
  });

  it("asks when the classifier fails", async () => {
    const before = gate({
      autoRun: () => true,
      classify: async () => {
        throw new Error("no uv");
      },
    });
    approval(await before(CALL, OWNER));
  });
});

describe("classifier.py through uv", () => {
  const script = join(dirname(fileURLToPath(import.meta.url)), "..", "classifier.py");
  const hasUv = (() => {
    try {
      execFileSync("uv", ["--version"], { stdio: "ignore" });
      return true;
    } catch {
      return false;
    }
  })();

  it("tells the two kinds of script apart", { skip: !hasUv && "uv is not installed" }, async () => {
    const classify = classifyWithPython(script, { PATH: process.env.PATH, HOME: process.env.HOME });
    assert.equal(await classify("ensure_device()\nprint(screen())"), null);
    assert.equal(await classify("import os\nos.system('id')"), "imports os");
  });

  it("rejects when it cannot run, so the gate asks", { skip: !hasUv && "uv is not installed" }, async () => {
    const classify = classifyWithPython(join(script, "missing.py"), { PATH: process.env.PATH, HOME: process.env.HOME });
    await assert.rejects(classify("print(1)"));
  });
});
