# Phone Harness 📱

**[Phone Harness](https://phone-harness.com?utm_source=github&utm_medium=readme&utm_campaign=header)** lets Claude Code, Codex or any agent control a real phone: open apps, tap, type, swipe and read the screen. An **iPhone** through the Mac's iPhone Mirroring window, an **Android** over adb on macOS, Linux or Windows, or a **cloud phone** over the internet. No jailbreak, no Xcode, nothing installed on the phone. Open source, MIT licensed.

```
  ● agent: wants to open Weather
  │
  ● find_text("Weather") → (400, 468)
  │
  ● tap(400, 468) → reads the screen → forecast is up
  ✓ done
```

No phone on your desk? Try [Phone Harness Cloud](https://phone-harness.com/cloud?utm_source=github&utm_medium=readme&utm_campaign=cloud): hosted iPhones and Androids, the first 100 minutes free.

## See it work

**Task:** "Buy me a Waymo to Delah Coffee from my current location."

https://github.com/user-attachments/assets/80b6d38e-0222-481c-93db-9de60d79247a

## Setup prompt

Paste into Claude Code or Codex:

```text
Install or upgrade phone-harness to the latest stable version with uv using Python 3.12, register the skill from `phone-harness skill`, then read https://github.com/ShawnPana/phone-harness/blob/main/onboarding.md and walk me through it. Follow https://github.com/ShawnPana/phone-harness/blob/main/install.md if setup or connection fails.
```

The agent asks which phone to use and walks you through the steps that need your hands: pairing iPhone Mirroring and granting Accessibility and Screen Recording, or turning on USB debugging. `phone-harness doctor` checks the setup.

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

Helpers are pre-imported. [SKILL.md](SKILL.md) is the agent's guide, [helpers.py](src/phone_harness/helpers.py) the full list. The agent keeps its own helpers in `~/.phone-harness/agent-workspace`.

## Cloud phones

```bash
phone-harness cloud login     # once, in your browser
phone-harness cloud ls        # phones on your account
phone-harness cloud start     # 15 minutes by default; --for 30m
phone-harness cloud stop
```

The same helpers drive a cloud phone: an Android over adb, an iPhone over HTTPS. Cloud Android bills by the minute. `phone-harness cloud` lists every command.

## MCP server

Phone Harness has a hosted MCP server at `https://mcp.phone-harness.com/mcp`. Add it as a connector in Claude, ChatGPT, Cursor or any MCP client and sign in. Or run it yourself with `uvx phone-harness-mcp`. Setup for every client: [github.com/phone-harness/mcp](https://github.com/phone-harness/mcp).

```bash
claude mcp add --transport http phone-harness https://mcp.phone-harness.com/mcp
```

> The MCP server drives cloud phones only. Local phone control is coming soon.

## How it works

- **iPhone:** captures the iPhone Mirroring window, reads it with Apple's Vision OCR, and sends taps and keystrokes as input events.
- **Android:** adb for screenshots and input, the accessibility tree for text. USB or Wi-Fi.
- **Cloud iPhone:** ops over HTTPS; the host runs OCR.

`phone-harness config set platform ios|android` picks the default. `phone-harness update` upgrades.

## Limits

- Unlocking the iPhone pauses mirroring; a PIN-locked Android needs you.
- OCR reads text, not icons. Unlabeled buttons need a screenshot and a vision model.
- No multi-touch, camera or Face ID. DRM video renders black.

## Sponsor

Phone Harness is free and maintained in my own time. [Sponsoring](https://github.com/sponsors/ShawnPana) keeps it that way.
