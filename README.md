# Phone Harness 📱

**[phone-harness](https://phone-harness.com?utm_source=github&utm_medium=readme&utm_campaign=header)** · let your agent control your phone.

Connect Claude Code, Codex, or any agent to your real phone. **iPhone** through
the Mac's iPhone Mirroring window, **Android** over adb from macOS, Linux or
Windows, or a Phone Harness Cloud phone — an Android over adb, or an iPhone
over HTTPS. No jailbreak, no
Xcode, nothing installed on the phone. The agent sees the screen, taps, types,
and reads the result.

```
  ● agent: wants to open Weather
  │
  ● find_text("Weather") → (400, 468)
  │
  ● tap(400, 468) → reads the screen → forecast is up
  ✓ done
```

Try [Phone Harness Cloud](https://phone-harness.com/cloud?utm_source=github&utm_medium=readme&utm_campaign=cloud) → Hosted iPhones and Androids with stealth, real numbers, 2FA, and unlimited devices

Get started by sending the [setup prompt](https://phone-harness.com?utm_source=github&utm_medium=readme&utm_campaign=setup-prompt) to your
coding agent.

## Demo

**Task:** "Buy me a Waymo to Delah Coffee from my current location."

https://github.com/user-attachments/assets/80b6d38e-0222-481c-93db-9de60d79247a

## Setup

Paste into Claude Code or Codex:

```text
Install or upgrade phone-harness to the latest stable version with uv using Python 3.12, register the skill from `phone-harness skill`, then read https://github.com/ShawnPana/phone-harness/blob/main/onboarding.md and walk me through it. Follow https://github.com/ShawnPana/phone-harness/blob/main/install.md if setup or connection fails.
```

The agent asks which phone is your default and walks you through the parts
that need your hands: pairing iPhone Mirroring and granting Accessibility and
Screen Recording, or turning on Android developer options and approving adb.
It also offers a cloud Android — the same helpers with nothing to pair, and
the first 100 minutes free; only cloud Android bills by the minute — so an iPhone user can test on Android too.
No Mac or no phone? The agent uses a cloud phone instead; nothing to install
but the CLI. `phone-harness doctor` checks the chain. Details in
[install.md](install.md) and [setup/](setup/).

**Hermes Agent:** `hermes plugins install ShawnPana/phone-harness/integrations/hermes`
adds a `phone_exec` tool and this skill. See [integrations/hermes](integrations/hermes/README.md).

## Usage

```bash
phone-harness <<'PY'
open_app("Notes")
tap_text("New Note")
type_text("hello from the harness")
print([o["text"] for o in screen()][:10])
PY
phone-harness --version
```

`phone-harness --version` (or `-V`) prints `phone-harness <version>`.

Helpers are pre-imported. [SKILL.md](SKILL.md) is the agent's day-to-day
guide; [helpers.py](src/phone_harness/helpers.py) is the full list.
Task-specific helpers live in `~/.phone-harness/agent-workspace`
(see [install.md](install.md)). The first run creates that folder from
the packaged defaults and never overwrites an existing one. Delete the
folder for a fresh default; the next run recreates it. A checkout uses
its own `agent-workspace/` instead.

## No phone on your desk? Rent one

```bash
phone-harness cloud login            # once: approve in your browser
phone-harness cloud ls               # phones on the account, and any live session
phone-harness cloud start            # cloud.phone if set, else a temporary Android; 15 min (--for 90s, 15m)
phone-harness cloud start ios        # the account's iPhone, when only one is listed
phone-harness <<'PY'
print(screenshot())
PY
phone-harness cloud stop             # ends the session; a saved Android is written back
```

`cloud ls` is the catalog (`GET /me` → `available`): each phone's label,
platform, and id, with a live session on that row (matched by device id,
profile id, or the temporary row). A cloud iPhone session does not echo its
catalog id (`device` is the word "iPhone", and `profile` is an `iphone-…` id
that matches no catalog row). That session is written on the one catalog row
of the same platform and kind whose `state` is `running`, when that is the
only such row and the only unmatched session of that platform and kind. A
session that matches nothing is its own row. `cloud start` with no name
starts this machine's `cloud.phone` setting if one is set, else what the
service gives an unnamed start, a temporary Android. A name is a catalog id or label, or `ios` /
`android` when only one entry has that platform. A [cloud Android](https://phone-harness.com/cloud)
is reached over adb, so every helper works on it unchanged. A cloud iPhone has
no shell: the helpers POST ops over HTTPS, OCR runs on the host, and driving
it is in [SKILL.md](SKILL.md). `phone-harness cloud` lists the rest, including
`watch`, `open`, `history`, and `phone` for the saved Android. A file goes to the phone with the `push_file(path)` helper (cloud iPhone only for now). `--session SID`
(or `PHONE_HARNESS_SESSION`) pins one rented phone for this process.

## MCP server

Prefer MCP? **Phone Harness** has a hosted MCP server at
`https://mcp.phone-harness.com/mcp`. Add it as a custom connector in Claude,
ChatGPT, Cursor or any MCP client and sign in; no key to paste, nothing to
install. Or run it yourself with `uvx phone-harness-mcp`. Source and setup for
every client: [github.com/phone-harness/mcp](https://github.com/phone-harness/mcp).

```bash
claude mcp add --transport http phone-harness https://mcp.phone-harness.com/mcp
```

> The MCP server drives cloud phones only. Local phone control (your iPhone
> through iPhone Mirroring, an Android over adb) is coming soon; until then,
> use the CLI above for the phone on your desk.

## Updates

`phone-harness update` upgrades a [uv](https://docs.astral.sh/uv/) tool
(`uv tool upgrade phone-harness`) or a git checkout (`git pull --ff-only`).
Any other install prints `pip install -U phone-harness` and leaves it for
you to run. The tool never updates itself. On a terminal, once a day, it
prints one line on stderr when PyPI has a newer version. Set
`PHONE_HARNESS_NO_UPDATE_CHECK=1` to silence that notice.

## How it works

**iPhone.** iPhone Mirroring renders the phone as a Mac window and forwards
mouse and keyboard as touches. The harness captures that window, OCRs it with
Apple's Vision framework for text with tap-ready coordinates, and posts
HID-level events for taps, swipes, and typing.

**Android.** adb is the transport. `screencap` is the capture, the phone's
accessibility tree is the text source, `input` is the hands. Works over USB or
Wi‑Fi, no window needed.

**Cloud iPhone.** `phone-harness cloud start` with that phone's catalog id,
label, or `ios` when it is the only one, drives it over HTTPS from any OS.
The host runs OCR; taps use the phone's screen points. No adb and no
accessibility tree. Day-to-day steps are in [SKILL.md](SKILL.md).

Same helpers throughout. `phone-harness config set platform ios|android` picks
the desk default.

## Limits

- Unlocking the iPhone pauses mirroring; a PIN-locked Android needs the user.
- OCR sees text, not icons. Unlabeled controls need a screenshot and a
  vision-capable model.
- No multi-touch, no camera or Face ID flows. DRM video renders black.
- Connecting the phone is always the user's job.

## Sponsor

phone-harness is free and maintained in my own time.
[Sponsoring](https://github.com/sponsors/ShawnPana) keeps it that way.
