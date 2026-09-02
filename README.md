# Phone Harness 📱

**[phone-harness](https://phone-harness.com)** · let your agent control your phone.

Connect Claude Code, Codex, or any agent to your real phone. **iPhone** through
the Mac's iPhone Mirroring window, **Android** over adb. No jailbreak, no
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

Get started by sending the [setup prompt](https://phone-harness.com) to your
coding agent.

Hosted iPhones and Androids with stealth, real numbers, and 2FA, as many as
you need. Try [Phone Harness Cloud](https://phone-harness.com/cloud).

## Demo

**Task:** "Buy me a Waymo to Delah Coffee from my current location."

<!-- demo video: paste the github user-attachments URL here -->

## Setup

Paste into Claude Code or Codex:

```text
Set up phone-harness for me. Clone https://github.com/ShawnPana/phone-harness into ~/.phone-harness, read `install.md` first, install it so `phone-harness` is a command on my PATH, and register it as an agent skill named phone-harness using `phone-harness skill` as the body. Then read `onboarding.md` and walk me through it.
```

The agent asks which phone is your default and walks you through the parts
that need your hands: pairing iPhone Mirroring and granting Accessibility and
Screen Recording, or turning on Android developer options and approving adb.
`phone-harness --doctor` checks the chain. Details in [install.md](install.md).

## Usage

```bash
phone-harness <<'PY'
open_app("Notes")
tap_text("New Note")
type_text("hello from the harness")
print([o["text"] for o in ocr()][:10])
PY
```

Helpers are pre-imported. [SKILL.md](SKILL.md) is the agent's day-to-day
guide; [helpers.py](src/phone_harness/helpers.py) is the full list.

## How it works

**iPhone.** iPhone Mirroring renders the phone as a Mac window and forwards
mouse and keyboard as touches. The harness captures that window, OCRs it with
Apple's Vision framework for text with tap-ready coordinates, and posts
HID-level events for taps, swipes, and typing.

**Android.** adb is the transport. `screencap` is the capture, the phone's
accessibility tree is the text source, `input` is the hands. Works over USB or
Wi‑Fi, no window needed.

Same helpers on both. `phone-harness config set platform ios|android` picks
the default.

## Limits

- One phone per session. Unlocking the iPhone pauses mirroring; a PIN-locked
  Android needs the user.
- OCR sees text, not icons. Unlabeled controls need a screenshot and a
  vision-capable model.
- No multi-touch, no camera or Face ID flows. DRM video renders black.
- Connecting the phone is always the user's job.

## Sponsor

phone-harness is free and maintained in my own time.
[Sponsoring](https://github.com/sponsors/ShawnPana) keeps it that way.
