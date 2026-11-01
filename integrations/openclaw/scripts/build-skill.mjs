// Build skills/phone-harness/SKILL.md from the repo's SKILL.md, which stays the only copy of the
// helper guide. OpenClaw reads a plugin's skills from fixed paths, so the published package carries
// the built file: OpenClaw frontmatter, how the guide maps onto phone_exec, then the guide itself.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const guide = readFileSync(join(here, "..", "..", "..", "SKILL.md"), "utf8");
const body = guide.replace(/^---\n[\s\S]*?\n---\n+/, "");
if (body === guide) throw new Error("SKILL.md has no frontmatter to replace");

// The one pin lives in the runner. The skill repeats it so sign-in uses the same release.
const runner = readFileSync(join(here, "..", "src", "phone-exec.ts"), "utf8");
const pin = runner.match(/PHONE_HARNESS_VERSION = "([^"]+)"/)?.[1];
if (!pin) throw new Error("src/phone-exec.ts has no PHONE_HARNESS_VERSION");

const skill = `---
name: phone-harness
description: "Control the user's phone: iPhone through iPhone Mirroring, Android over adb, or a cloud phone. Open apps, tap, type, swipe, read the screen."
homepage: https://phone-harness.com
metadata:
  {
    "openclaw":
      {
        "emoji": "📱",
        "requires": { "bins": ["uv"] },
        "envVars":
          [
            {
              "name": "PHONE_HARNESS_API_KEY",
              "required": false,
              "description": "Phone Harness Cloud key, for machines where nobody can run cloud login.",
            },
          ],
        "install":
          [
            {
              "id": "uv",
              "kind": "brew",
              "formula": "uv",
              "bins": ["uv"],
              "label": "Install uv (brew)",
            },
          ],
      },
  }
---

# Phone Harness on OpenClaw

Run every script with the \`phone_exec\` tool: put the Python in \`code\` and print() what you need
back. Where the guide below shows \`phone-harness <<'PY'\`, the lines of that heredoc are the \`code\`.
Each call asks the user first, unless they allowed this session.

There may be no \`phone-harness\` command on this machine. Run the guide's \`phone-harness cloud ...\`
commands inside \`phone_exec\`, and add \`--no-watch\`, because a live view would open on the Gateway
machine and not in front of the user:

\`\`\`python
from phone_harness.cloud import cli
cli(["start", "Temporary phone", "--for", "10m", "--no-watch"])
\`\`\`

A cloud Android bills the user by the minute, so ask before \`start\` and run \`cli(["stop"])\` when
the task is done. If the phone reports \`Not signed in\`, ask the user to run this once in a terminal
on the Gateway machine:

\`\`\`bash
uvx --from phone-harness==${pin} phone-harness cloud login
\`\`\`

${body}`;

const target = join(here, "..", "skills", "phone-harness", "SKILL.md");
mkdirSync(dirname(target), { recursive: true });
writeFileSync(target, skill);
