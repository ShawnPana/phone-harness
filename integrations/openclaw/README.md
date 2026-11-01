# phone-harness for OpenClaw

An [OpenClaw](https://openclaw.ai) plugin that lets your agent drive a real phone
with one tool, `phone_exec`. The model writes a short Python script with the
phone-harness helpers pre-imported. You approve it, and the plugin runs it on the
phone and returns what it printed.

```text
phone_exec(code='''
ensure_device()
open_app("Weather")
wait_stable()
print([o["text"] for o in screen()][:12])
''')
```

The phone can be your iPhone through the Mac's iPhone Mirroring window, an Android
over adb, or a [Phone Harness Cloud](https://phone-harness.com/dashboard) phone.

## Install

You need OpenClaw 2026.10.9 or newer, and [uv](https://docs.astral.sh/uv/getting-started/installation/)
on the Gateway host.

The plugin is not on ClawHub yet, so build it from this repo:

```bash
git clone https://github.com/ShawnPana/phone-harness
cd phone-harness/integrations/openclaw
npm install
npm pack --pack-destination /tmp
openclaw plugins install npm-pack:/tmp/phone-harness-openclaw-plugin-0.1.0.tgz
```

OpenClaw shows what the plugin can do and asks you to accept it. `phone_exec` is an
optional tool, so the model cannot see it until you allow it. This command replaces
`tools.alsoAllow`, so include anything already in that list:

```bash
openclaw config set tools.alsoAllow '["phone_exec"]' --strict-json
```

Start a new chat, then ask for something on your phone.

phone-harness chooses the phone: an attached cloud phone, otherwise its default. To
force the phone on this machine, export `PHONE_HARNESS_PLATFORM` as `ios` or
`android` in the environment the Gateway runs in.

- **iPhone:** macOS 15+ and iPhone Mirroring paired to an iOS 18+ iPhone. Give
  Accessibility and Screen Recording to the app that runs the Gateway (your terminal
  for `openclaw gateway run`). Keep the phone locked while the agent works.
- **Android:** adb, and USB debugging on the phone.
- **Cloud phone:** sign in once on the Gateway host:

  ```bash
  uvx --from phone-harness==0.3.2 phone-harness cloud login
  ```

[install.md](../../install.md) covers each.

## Approvals

Every `phone_exec` call asks before it runs. The prompt shows the script: the whole
of a short one, or the start and end of a long one with a count of what is hidden.
You answer **Allow once** or **Deny** in the Control UI, or with `/approve` in a chat
that supports it. See [plugin permission requests](https://docs.openclaw.ai/plugins/plugin-permission-requests)
to send prompts to a chat channel.

- There is no "allow always". A script is arbitrary Python on your computer, so no
  answer covers the next one. The agent is told to do a whole step in each script.
- If nobody can answer (a scheduled job, or a CLI run with no approval surface), the
  script does not run.
- What to do on the phone itself (sending, posting, buying, deleting, changing
  settings) is the skill's consent rule: the agent asks you first.

## How it works

1. The model calls `phone_exec(code, timeout_s=300)`, directly or through OpenClaw's
   Tool Search.
2. A `before_tool_call` hook asks you to approve the script. The tool then runs only
   the exact script you approved for that exact call.
3. Sessions and subagents take turns on the phone. After 120 s the tool reports the
   phone as busy.
4. The plugin pipes the script on stdin to the pinned phone-harness, run with
   `uvx --from phone-harness==<pin>`, so it stays apart from any `phone-harness` on
   your `PATH`. The pin is one line, in [src/pin.ts](src/pin.ts).
5. The result is what the script printed, the exit code, and the tail of stderr. The
   last image the script saved and printed is attached, reduced to 1200 px, when the
   model can view images. A script past its timeout is killed with everything it
   started.

The plugin also ships the phone-harness skill, built from the pinned release with
`npm run build`.

## Privacy and the environment

- Telemetry is off for plugin runs unless you opt in with
  `phone-harness config set telemetry true`.
- The script does not get the Gateway's credentials. Variables named like
  `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD` and `*_PRIVATE_KEY` are removed,
  and any `PHONE_HARNESS_*` you exported is kept. This matches names, so a secret
  stored under another name is not removed.
- The agent workspace is `phone-harness/agent-workspace` in OpenClaw's state folder.
  If `PH_AGENT_WORKSPACE` is set, that path is used instead.

## What it touches

- Runs model-written Python on the Gateway host. It is not a sandbox: approve only
  what you would run yourself. Sessions in an OpenClaw sandbox do not get the tool,
  because it would run outside the sandbox.
- Taps and types into your real phone, with your apps and accounts.
- Screenshots are PNG files in the system temp folder.
- A cloud phone streams its screen to Phone Harness Cloud and uses phone-harness's
  own sign-in. A cloud Android bills by the minute; a cloud iPhone does not.
- Allowing the tool for an agent lets anyone who can message that agent ask for a
  phone script. You still approve each one.

## Limits

- No password-vault integration: the agent never types passwords, PINs or 2FA codes.
  Hand the phone back to the user for those.
- `back()` is Android-only; on an iPhone the agent taps the app's back arrow.
- Unlocking your iPhone pauses mirroring. The agent asks you to lock it.

## Development

```bash
npm install
npm run build
npm test
npx clawhub package validate .
```

To move to a new phone-harness release, change [src/pin.ts](src/pin.ts) and rebuild.

## OpenClaw docs this follows

- Packaging: [Building plugins](https://docs.openclaw.ai/plugins/building-plugins),
  [Tool plugins](https://docs.openclaw.ai/plugins/tool-plugins),
  [definePluginEntry](https://docs.openclaw.ai/plugins/sdk-entrypoints/define-plugin-entry),
  [Plugin manifest](https://docs.openclaw.ai/plugins/manifest),
  [manifest capability fields](https://docs.openclaw.ai/plugins/manifest/capabilities),
  [Manifest versus package.json](https://docs.openclaw.ai/plugins/manifest/package-json)
- Safety: [Plugin permission requests](https://docs.openclaw.ai/plugins/plugin-permission-requests),
  [Tool call policy hooks](https://docs.openclaw.ai/plugins/hooks/tool-policy),
  [State and system runtime](https://docs.openclaw.ai/plugins/sdk-runtime/state-and-system)
- Skills: [Skills](https://docs.openclaw.ai/tools/skills)
- Publishing: [Community plugins](https://docs.openclaw.ai/plugins/community),
  [ClawHub publishing](https://docs.openclaw.ai/clawhub/publishing),
  [ClawHub CLI](https://docs.openclaw.ai/clawhub/cli),
  [Plugin validation fixes](https://docs.openclaw.ai/clawhub/plugin-validation-fixes),
  [Security audits](https://docs.openclaw.ai/clawhub/security-audits),
  [Acceptable usage](https://docs.openclaw.ai/clawhub/acceptable-usage)
