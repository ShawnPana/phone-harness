# Phone Harness 📱

**[phone-harness](https://phone-harness.com?utm_source=github&utm_medium=readme&utm_campaign=header)** · let your agent control your phone.

Connect Claude Code, Codex, or any agent to your real phone. **iPhone** from
macOS, Linux or Windows — through the Mac's iPhone Mirroring window, or over a
USB cable from any OS. **Android** over adb from anywhere. No jailbreak, no
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
Set up phone-harness for me. Clone https://github.com/ShawnPana/phone-harness into ~/.phone-harness, read `install.md` first, install it so `phone-harness` is a command on my PATH, and register it as an agent skill named phone-harness using `phone-harness skill` as the body. Then read `onboarding.md` and walk me through it.
```

The agent asks which phone is your default and walks you through the parts
that need your hands: pairing iPhone Mirroring and granting Accessibility and
Screen Recording on a Mac; trusting the computer and turning on Developer Mode
for an iPhone over USB; or turning on Android developer options and approving
adb. `phone-harness --doctor` checks the chain. Details in [install.md](install.md).

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

**iPhone on a Mac.** iPhone Mirroring renders the phone as a Mac window and
forwards mouse and keyboard as touches. The harness captures that window, OCRs
it with Apple's Vision framework for text with tap-ready coordinates, and posts
HID-level events for taps, swipes, and typing.

**iPhone over USB (Linux, Windows, macOS).** The same developer services Xcode
uses, reached through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3):
a screenshot service is the eyes, a virtual HID touchscreen and keyboard are
the hands, the pasteboard service carries pasted text, the app service
launches apps by bundle id. No window at all. Needs iOS 27+, Developer Mode,
and, until it is released, an install from the checkout with the `iphone`
extra on Python 3.13+ (see install.md).

**Android.** adb is the transport. `screencap` is the capture, the phone's
accessibility tree is the text source, `input` is the hands. Works over USB or
Wi‑Fi, no window needed.

Same helpers on all three. `phone-harness config set platform ios|android`
picks the default.

## Limits

- Unlocking the iPhone pauses mirroring on a Mac; over USB the phone stays in
  your hands but a locked phone still needs you to unlock it. A PIN-locked
  Android needs the user too.
- iPhone over USB needs iOS 27 or later and Developer Mode on.
- OCR sees text, not icons. Unlabeled controls need a screenshot and a
  vision-capable model.
- No multi-touch, no camera or Face ID flows. DRM video renders black.
- Connecting the phone is always the user's job.

## Sponsor

phone-harness is free and maintained in my own time.
[Sponsoring](https://github.com/sponsors/ShawnPana) keeps it that way.
