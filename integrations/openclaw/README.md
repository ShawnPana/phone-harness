# Phone Harness for OpenClaw

An [OpenClaw](https://docs.openclaw.ai) plugin that lets your agent drive a real
phone with one tool, `phone_exec`. The model writes a short Python script with
the Phone Harness helpers pre-imported, you approve it, and the plugin runs it on
the phone and returns what it printed.

```text
phone_exec(code='''
ensure_device()
open_app("Settings")
wait_stable()
print([o["text"] for o in screen()][:12])
''')
```

The plugin ships the `phone-harness` skill, which teaches the agent the helpers.

## Install

You need OpenClaw 2026.10.9 or newer and [uv](https://docs.astral.sh/uv/) on the
machine that runs the Gateway.

```bash
openclaw plugins install clawhub:phone-harness
```

OpenClaw shows what the plugin can do and asks you to accept it. A running
Gateway picks the plugin up straight away, so there is nothing to restart. The
first script fetches Phone Harness through uv, which takes a moment once.

Until the plugin is on ClawHub, install it from a checkout:

```bash
git clone https://github.com/ShawnPana/phone-harness
cd phone-harness/integrations/openclaw
npm install --ignore-scripts && npm pack
openclaw plugins install npm-pack:./phone-harness-0.1.0.tgz
```

To use a cloud phone, sign in once on the Gateway machine:

```bash
uvx phone-harness cloud login
```

Phone Harness chooses the phone: an attached cloud phone, otherwise its default.
To force the phone on this machine, export `PHONE_HARNESS_PLATFORM` as `ios` or
`android` in the environment the Gateway runs in. iPhone control needs
Accessibility and Screen Recording for the app that runs the Gateway. Android
needs adb. [install.md](../../install.md) covers each.

## Which agents get the tool

- The `coding` and `full` tool profiles include `phone_exec`, as does a config
  with no profile.
- The `messaging` and `minimal` profiles do not, so a chat bot does not offer your
  phone by default. To add it, set `tools.alsoAllow` to `["phone_exec"]`.
- Sandboxed sessions never get the tool. A plugin runs inside the Gateway, so a
  script would run on the Gateway machine, outside the sandbox.

## Approvals

Every `phone_exec` call asks you before it runs. You see the script, or both ends
of it if it is long, and choose:

- **Allow once** runs this script.
- **Always allow** runs this script and every later script in the same session.
  It ends with the session, and a new conversation, a plugin reload or a Gateway
  restart asks again.
- **Deny** stops it.

Approve from the Control UI, or in a chat with `/approve`. If nobody can answer,
the script does not run. That includes `openclaw agent` from a terminal and
scheduled jobs. A prompt nobody answers expires after five minutes.

Only the owner can drive the phone. OpenClaw lets a chat answer its own approval
prompts, so a sender it knows is not the owner is refused before any prompt.

## Settings

- Classifier auto-run is off unless you opt in. With it on, a script the
  classifier judges to be phone-only runs without asking. The classifier is a
  heuristic and can be bypassed, so a script may still reach this computer. Leave
  it off unless you accept that. `openclaw security audit` reports it when it is on.
  - Plugin setting `plugins.entries.phone-harness.config.classifierAutoRun`
    (boolean, default false).
  - Or export `PHONE_HARNESS_CLASSIFIER_AUTO_RUN=1` in the environment the Gateway
    runs in. When that variable is set, it wins over the plugin setting.

  While auto-run is on, a script still asks when it imports anything other than
  the phone helpers, reads or writes files, runs or evaluates code, uses Python
  internals, runs an adb shell command, or calls `send("raw", ...)`. What to do on
  the phone itself (sending, posting, buying, deleting, changing settings) is still
  the skill's consent rule: the agent asks you first.
- Telemetry is off for plugin runs unless you opt in with
  `phone-harness config set telemetry true`.
- `PHONE_HARNESS_PLATFORM`, when you have exported it, is passed through.
- The agent workspace is `phone-harness/agent-workspace` in OpenClaw's state
  directory, or `PH_AGENT_WORKSPACE` if that is set.

## How it works

1. The model calls `phone_exec(code, timeout_s=300)`. The limit is 1800 seconds.
2. A `before_tool_call` hook asks you to approve the script.
3. Subagents take turns on the phone. After 120 s the tool reports it as busy.
4. The plugin pipes the script to the pinned Phone Harness through `uvx`, with
   `PH_AGENT_WORKSPACE` and `PH_CLIENT=openclaw`. The script's environment leaves
   out `OPENCLAW_*` and anything named like a credential (API keys, tokens,
   secrets, passwords), but keeps any `PHONE_HARNESS_*` you exported.
5. The result is what the script printed, plus the exit code and the tail of
   stderr. A screenshot saved during the call is attached for the model to see.
   If the file is not a real image, the result says so and nothing is attached. A
   script past its timeout, or a cancelled call, is killed with everything it
   started.

## What it touches

- Runs model-written Python on the Gateway machine, as the user running OpenClaw.
- Taps and types into your real phone, with your apps and accounts.
- Screenshots are PNG files in the system temp folder.
- A cloud phone streams its screen to Phone Harness Cloud and uses Phone Harness's
  own cloud sign-in. A rented cloud Android bills by the minute.

## Limits

- The approval covers `phone_exec`. An agent that also has OpenClaw's `exec` tool
  and a `phone-harness` command can reach the phone through the shell, under your
  exec approvals instead. Deny `exec` for that agent if you want every phone script
  to come through this gate.
- No password-vault integration: the agent never types passwords, PINs or 2FA
  codes. Hand the phone back to the user for those.
- `back()` is Android-only; on an iPhone the agent taps the app's back arrow.

## Development

```bash
npm install
npm run build   # dist/ and skills/, built from the repo's SKILL.md
npm test
```

The Phone Harness release is pinned on one line: `PHONE_HARNESS_VERSION` at the
top of `src/phone-exec.ts`. `classifier.py` is the Hermes plugin's classifier, and
`tests/test_openclaw_plugin.py` keeps the two identical.
