# phone-harness for Hermes Agent

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets
Hermes drive a real phone with one tool, `phone_exec`. It has the same shape as
Hermes's own `browser_exec`: the model writes a short Python script with the
phone-harness helpers pre-imported, and the plugin runs it on the phone and
returns what it printed.

```text
phone_exec(code='''
ensure_device()
open_app("Weather")
wait_stable()
print([o["text"] for o in ocr()][:12])
''')
```

It also registers phone-harness's [SKILL.md](../../SKILL.md) as the skill
`phone-harness:phone-harness`, so the agent knows how to read the screen, act
and verify before its first script.

## Install

```bash
hermes plugins install ShawnPana/phone-harness/integrations/hermes
```

Hermes asks to install the `phone-harness` package into its environment and
whether to enable the plugin. Tools load per session, so start a new chat.

Then pick the phone. On a Mac with iPhone Mirroring paired:

```bash
hermes config set plugins.entries.phone-harness.settings.platform ios
```

iPhone control needs Accessibility and Screen Recording for the app that runs
Hermes (the Hermes desktop app, or your terminal for `hermes` in a shell), not
only for the terminal you use phone-harness from. Android needs adb; a cloud
phone needs `phone-harness cloud login`. [install.md](../../install.md) covers
each.

## Settings

Desktop: **Settings → Plugins → phone-harness**. CLI:
`hermes config set plugins.entries.phone-harness.settings.<key> <value>`.

| Key | Default | What it does |
|---|---|---|
| `platform` | `auto` | `auto` lets phone-harness choose (an attached cloud phone, else its default). `ios` or `android` drives the phone on this machine on purpose, which is also what phone-harness requires after a rented cloud phone has gone. |
| `telemetry` | `false` | Off: scripts run with `PHONE_HARNESS_TELEMETRY=0`. On: phone-harness's own telemetry setting applies. |
| `agent_workspace` | empty | Folder with `agent_helpers.py`, passed as `PH_AGENT_WORKSPACE`. Empty: `~/.phone-harness/agent-workspace` if that checkout exists, else a folder in Hermes's plugin data. |

## How it works

1. The model calls `phone_exec(code, timeout_s=300)`.
2. A `pre_tool_call` hook reads the script. Phone helpers and plain data
   handling go straight through. A script that reaches past the phone goes to
   Hermes's approval prompt first (see below).
3. The plugin waits for the phone: one script at a time, across subagents and
   across Hermes processes (Desktop, CLI, cron), via a lock file in Hermes's
   plugin data. After 120 s it reports the phone as busy.
4. It pipes the script on stdin to `phone-harness`: your own CLI when it is on
   `PATH` or in `~/.local/bin`, else the copy Hermes installed, run on Hermes's
   interpreter. The script runs in the agent workspace with
   `PH_AGENT_WORKSPACE`, `PH_CLIENT=hermes`, the `platform` setting and, unless
   you opted in, `PHONE_HARNESS_TELEMETRY=0`.
5. The result is what the script printed, plus the exit code, the tail of
   stderr, and any screenshot paths it printed (the agent opens those with
   `vision_analyze`). A script past its timeout is killed with everything it
   started.

## Approvals

phone-harness scripts are Python, so they can do anything Python can on this
computer. The hook sends a script to Hermes's approval prompt when it:

- imports anything beyond `phone_harness` and a small set of data modules
  (`json`, `re`, `time`, `math`, `datetime`, `collections`, ...);
- calls `open`, `exec`, `eval`, `compile`, `__import__`, `breakpoint` or `input`;
- touches dunder names (`__builtins__`, `__globals__`, `"__self__"`, ...);
- runs an adb `shell()` command on the phone.

Denial, timeout or no one to ask (cron, the messaging gateway) fails closed.
This is a heuristic gate, not a sandbox. Taps, typing and app launches never
ask: what to do on the phone is the skill's consent rule, which tells the agent
to ask before sending, posting, buying, deleting or changing settings.

## What it touches

- Runs model-written Python locally through the phone-harness CLI.
- Taps and types into your real phone, with your apps and accounts.
- Screenshots are PNG files in the system temp folder.
- A cloud phone streams its screen to Phone Harness Cloud, bills by the minute,
  and uses phone-harness's own cloud sign-in.
- Hermes adds the plugin's toolset to every platform that has a saved tool
  selection, messaging platforms included. Turn it off there with
  `hermes tools` unless you want people who can message your bot to drive your
  phone.

## Limits

- Screenshots are not attached to the model's context automatically; the
  agent views them with `vision_analyze`.
- No password-vault integration: the agent never types passwords, PINs or 2FA
  codes. Hand the phone back to the user for those.
- `back()` is Android-only; on an iPhone the agent taps the app's back arrow.
