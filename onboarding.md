# Onboarding

You are setting up phone-harness with the user, once. Ask as little as
possible: detect what is already in place, and stop only for the steps that
need the user's hands. Tell them exactly what to do on the phone or in System
Settings, then wait.

## 1. One question

"Which phone should be your default — iPhone or Android?" (Both is fine: set
up each, then ask which is the default.)

## 2. iPhone

Two ways to drive an iPhone. Prefer the first; it works on every OS and ends
with a live preview in the browser.

**Detect first, before any doctor.** Do not start with `--doctor ios` on a
Mac: on a Mac that means iPhone Mirroring, and it will send you down the
fallback path. Instead install the extra from the checkout
(`cd ~/.phone-harness && uv tool install --python 3.13 --editable ".[iphone]"`,
never `phone-harness[iphone]` from PyPI — that release lacks this backend;
on Linux also `usbmuxd`, on Windows iTunes or the Apple Devices app), plug the
phone in and unlock it, then run `phone-harness ios`. It prints the phone,
its iOS version, whether this computer is trusted, Developer Mode, and any
saved Wi-Fi pairing. If it prints usage instead, the PyPI install is
shadowing the checkout; see install.md.

- **iOS 27 or newer on USB, or a saved Wi-Fi pairing → CoreDevice.** Follow
  the steps below.
- **iOS below 27 on a Mac → iPhone Mirroring.** See the fallback at the end.
  On Linux or Windows there is no fallback; say so and stop.

**CoreDevice setup**, stopping for the user's hands at each step:

- Not trusted yet: `phone-harness ios pair` → they tap **Trust** and enter
  the passcode on the phone. Never ask for the passcode.
- **Developer Mode.** Explain first: it lets trusted computers use the
  phone's developer services and loosens its security posture; turning it
  off later does not remove the trust. Ask whether they want to continue.
  Then: Settings → Privacy & Security → Developer Mode → on → the phone
  restarts → confirm. If the switch is not listed, `phone-harness ios reveal`
  makes it appear (close and reopen Settings).
- Make it the default: `phone-harness config set platform coredevice` on a
  Mac (`ios` there means iPhone Mirroring); `phone-harness config set
  platform ios` on Linux or Windows.
- Optional, once, while still plugged in: `phone-harness ios pair --wifi`,
  so later sessions work with no cable when the phone is on the same Wi-Fi.
- **The "you are done" moment:** `phone-harness ios mirror`. It starts the
  session (the first time it downloads and mounts Apple's developer disk
  image, which takes a moment) and opens the phone's live screen in the
  browser. Have the user click something on it. `phone-harness ios rest`
  ends the session; `phone-harness ios awake --bg` starts one without the
  preview.

Check with `phone-harness --doctor coredevice` (or `--doctor ios` off a Mac).

**Fallback, Mac only, iOS below 27: iPhone Mirroring.** Two things only the
user can do:

- Pair iPhone Mirroring with the phone once (open the app; the pairing prompts
  need the physical phone).
- Grant the terminal **Accessibility** and **Screen Recording** in System
  Settings → Privacy & Security (Screen Recording takes effect after the
  terminal restarts).

Check with `phone-harness --doctor ios` and only ask for what is missing.
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

`phone-harness --doctor` for the default phone (add `coredevice`, `ios` or
`android` to check another), then a read-only proof: take a screenshot and
read the screen back to the user. On CoreDevice the live preview from
`phone-harness ios mirror` is the proof.

## 5. Demo (opt-in)

Ask whether to open phone-harness.com on the phone (Safari on iPhone, Chrome
on Android), tap "Star on GitHub" and star the repo for them — only if they say
yes. If the phone is locked or the session is paused, report the doctor status
instead.

## Rules

Never type a PIN or passcode. Never change a phone setting without asking —
including Developer Mode, which only the user turns on. Cabling, unlocking
and trusting the phone are the user's job — relay the doctor's message and
wait; don't retry-loop. Everything after that (starting the USB session,
mounting the developer image, opening the mirror) is yours: the helpers start
the session themselves, so just proceed. After this, day-to-day usage is
`SKILL.md`; setup reference and troubleshooting are `install.md`.
