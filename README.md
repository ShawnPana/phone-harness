<div align="center">
  <a href="https://phone-harness.com?utm_source=github&utm_medium=readme&utm_campaign=header">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/logo-animated-dark.svg?v=6f742c0">
      <img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/logo-animated.svg?v=6f742c0" alt="phone-harness" width="560">
    </picture>
  </a>
  <br>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/tagline-dark.svg?v=6f742c0">
    <img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/tagline.svg?v=6f742c0" width="600" height="48" alt="Let your agent use your phone.">
  </picture>
</div>

---

<p>
  <a href="https://phone-harness.com/docs"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-docs-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-docs.svg?v=6f742c0" width="98" height="40" align="top" alt="Docs"></picture></a>
  <a href="https://phone-harness.com/cloud?utm_source=github&utm_medium=readme-button-cloud"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-cloud-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-cloud.svg?v=6f742c0" width="104" height="40" align="top" alt="Phone Harness Cloud"></picture></a>
  <a href="https://phone-harness.com/cloud?utm_source=github&utm_medium=readme-callout"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/callout-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/callout.svg?v=6f742c0" width="230" height="40" align="top" alt="Try your own cloud Android phone for free!"></picture></a>
  <a href="https://discord.gg/7FwGsjvjS"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-discord-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-discord.svg?v=6f742c0" width="32" height="40" align="right" alt="Discord"></picture></a><a href="https://www.linkedin.com/company/phone-harness"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-linkedin-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-linkedin.svg?v=6f742c0" width="32" height="40" align="right" alt="LinkedIn"></picture></a><a href="https://github.com/ShawnPana/phone-harness"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-github-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-github.svg?v=6f742c0" width="32" height="40" align="right" alt="GitHub"></picture></a><a href="https://x.com/phone_harness"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-x-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/icon-x.svg?v=6f742c0" width="32" height="40" align="right" alt="X"></picture></a>
</p>

---

Enable Claude Code, Codex, or any agent to use your real phone. The agent can
scroll, tap, type, and read your phone with no jailbreak, no Xcode, and nothing
installed on the phone.

> **iPhone** through the Mac's iPhone Mirroring window <br/>
> **Android** over adb from macOS, Linux or Windows <br/>
> **Phone Harness Cloud** phone: an Android over adb, or an iPhone over HTTPS

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

<img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/demo-waymo.gif?v=6f742c0" width="100%" alt="An agent orders a Waymo to Delah Coffee on a real iPhone through phone-harness">

## Setup

For the CLI, send this setup prompt to your agent (Claude Code, Codex, Grok Bot, and more):

```text
Install or upgrade phone-harness to the latest stable version with uv using Python 3.12, register the skill from `phone-harness skill`, then read https://github.com/ShawnPana/phone-harness/blob/main/onboarding.md and walk me through it. Follow https://github.com/ShawnPana/phone-harness/blob/main/install.md if setup or connection fails.
```

The agent installs the CLI, asks which phone to use, and walks you through the steps that need your hands. What each phone needs:

| Phone | Needs |
| --- | --- |
| **iPhone** | macOS 15+, iPhone Mirroring paired to an iOS 18+ iPhone, Accessibility and Screen Recording permission |
| **Android** | adb and USB debugging, on macOS, Linux or Windows |
| **Cloud Android / iPhone** | [Phone Harness Cloud](https://phone-harness.com/cloud) account; cloud Android also needs adb |

`phone-harness doctor` checks the setup. Full details: [install.md](install.md) and [setup/](setup/).

**MCP:** add `https://mcp.phone-harness.com/mcp` to any MCP client ([setup](https://github.com/phone-harness/mcp)). Cloud Android and iPhone only for now.

**Hermes Agent:** `hermes plugins install ShawnPana/phone-harness/integrations/hermes`. See [integrations/hermes](integrations/hermes/README.md).

## Usage

```bash
phone-harness <<'PY'
open_app("Notes")
tap_text("New Note")
type_text("hello from the harness")
print([o["text"] for o in screen()][:10])
PY
```

Helpers are pre-imported; [helpers.py](src/phone_harness/helpers.py) has the full list. The agent saves its own helpers in `~/.phone-harness/agent-workspace`.

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
