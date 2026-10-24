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
  <a href="https://phone-harness.com/docs?utm_source=github&utm_medium=readme&utm_campaign=button-docs"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-docs-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-docs.svg?v=6f742c0" width="98" height="40" align="top" alt="Docs"></picture></a>
  <a href="https://phone-harness.com/cloud?utm_source=github&utm_medium=readme&utm_campaign=button-cloud"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-cloud-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/button-cloud.svg?v=6f742c0" width="104" height="40" align="top" alt="Phone Harness Cloud"></picture></a>
  <a href="https://phone-harness.com/cloud?utm_source=github&utm_medium=readme&utm_campaign=callout"><picture><source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/callout-dark.svg?v=6f742c0"><img src="https://raw.githubusercontent.com/phone-harness/media/main/phone-harness/callout.svg?v=6f742c0" width="230" height="40" align="top" alt="Try your own cloud Android phone for free!"></picture></a>
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
| **Cloud Android / iPhone** | [Phone Harness Cloud](https://phone-harness.com/cloud?utm_source=github&utm_medium=readme&utm_campaign=setup-table) account; cloud Android also needs adb |

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

## Phone Harness Cloud

No phone on your desk, or need more than one? [Phone Harness Cloud](https://phone-harness.com/cloud?utm_source=github&utm_medium=readme&utm_campaign=cloud-section) hosts them for you:

- **Android phones** on demand: a fresh one per session, or a saved phone that keeps its apps and logins.
- **Dedicated real iPhones**, driven over HTTPS from any OS.
- **A live view** of the phone in your browser while the agent works.

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

The same helpers drive a cloud phone: an Android over adb, an iPhone over HTTPS. `cloud start` takes a phone's name or id from `cloud ls`, or `ios` / `android`; with none it starts a temporary Android. `phone-harness cloud` lists every command.

## How it works

Every phone sits behind the same helpers, so a script written for one runs on the others.

- **iPhone:** captures the iPhone Mirroring window, reads it with Apple's Vision OCR, and sends taps, swipes and typing as input events.
- **Android:** adb for screenshots and input, the accessibility tree for text. USB or Wi-Fi.
- **Cloud Android:** the same as Android, over adb to a hosted phone.
- **Cloud iPhone:** ops over HTTPS.

`phone-harness config set platform ios|android` picks the default phone. `phone-harness update` upgrades the CLI, and it prints one line when a new version is out (`PHONE_HARNESS_NO_UPDATE_CHECK=1` silences it).

## Limits

- Unlocking the iPhone pauses mirroring; a PIN-locked Android needs the user.
- OCR sees text, not icons. Unlabeled controls need a screenshot and a
  vision-capable model.
- No multi-touch, no camera or Face ID flows. DRM video renders black.
- Connecting the phone is always the user's job.

## Sponsor

phone-harness is free and maintained in my own time.
[Sponsoring](https://github.com/sponsors/ShawnPana) keeps it that way.
