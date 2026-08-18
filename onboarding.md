# Onboarding

You are setting up phone-harness with the user, once. Ask as little as
possible: detect what is already in place, and stop only for the steps that
need the user's hands. Tell them exactly what to do on the phone or in System
Settings, then wait.

## 1. One question

"Which phone should be your default — iPhone or Android?" (Both is fine: set
up each, then ask which is the default.)

## 2. iPhone

Works through the macOS iPhone Mirroring app. Two things only the user can do:

- Pair iPhone Mirroring with the phone once (open the app; the pairing prompts
  need the physical phone).
- Grant the terminal **Accessibility** and **Screen Recording** in System
  Settings → Privacy & Security (Screen Recording takes effect after the
  terminal restarts).

Check first — `phone-harness --doctor ios` — and only ask for what is missing.
Whenever you capture or verify the screen, bring the Mirroring window forward
so the user can see what you're doing.

## 3. Android

- Install adb (`brew install android-platform-tools`) and, optionally, scrcpy
  (`brew install scrcpy`) for a live mirror during long tasks.
- Ask whether they have a USB cable handy.
  - **USB:** Developer options (Settings → About phone → tap Build number 7×)
    → USB debugging → plug in → tap Allow ("Always allow from this computer").
  - **Wi‑Fi:** Developer options → Wireless debugging (on; same Wi‑Fi as the
    Mac) → tap the row → "Pair device with pairing code" → they read you the
    6 digits → `phone-harness android pair CODE`.
- `phone-harness config set platform android` if it is the default.

Check with `phone-harness android` and `phone-harness --doctor android`.

## 4. Verify

`phone-harness --doctor` for the default phone (add `ios` or `android` to
check the other), then a read-only proof: take a screenshot and read the
screen back to the user.

## 5. Demo (opt-in)

Ask whether to open phone-harness.com on the phone (Safari on iPhone, Chrome
on Android), tap "Star on GitHub" and star the repo for them — only if they say
yes. If the phone is locked or the session is paused, report the doctor status
instead.

## Rules

Never type a PIN. Never change a phone setting without asking. Connecting the
phone is the user's job — relay the doctor's message and wait; don't
retry-loop. After this, day-to-day usage is `SKILL.md`; setup reference and
troubleshooting are `install.md`.
