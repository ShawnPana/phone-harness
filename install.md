# phone-harness install

phone-harness drives a real phone from your computer (first-run flow for
agents: `onboarding.md`; day-to-day usage: `SKILL.md`). It works with an
**iPhone** — through the macOS iPhone Mirroring app on a Mac, or over a USB
cable from Linux, Windows or macOS — or an **Android** over adb (USB or Wi‑Fi).
Same helpers either way; you choose a default and can switch per call.

## Common

```bash
git clone https://github.com/ShawnPana/phone-harness ~/.phone-harness   # canonical home
cd ~/.phone-harness
pip install -e .                      # the global `phone-harness` command (pulls pyobjc on macOS only)

# register as an agent skill so Claude Code / Codex reach for it automatically
mkdir -p ~/.claude/skills/phone-harness
phone-harness skill > ~/.claude/skills/phone-harness/SKILL.md
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/phone-harness"
phone-harness skill > "${CODEX_HOME:-$HOME/.codex}/skills/phone-harness/SKILL.md"
```

- Python 3.10+, any OS (3.13+ for the USB iPhone path). **Android works on
  macOS, Linux and Windows** and is the default off a Mac. **iPhone** works on
  any OS over USB or Wi-Fi (see "iPhone over USB" below; prefer it), and on a
  Mac through iPhone Mirroring for iOS below 27.
  Only the CLI? `pip install phone-harness` works too for Android and iPhone
  Mirroring, but not yet for the USB/Wi-Fi iPhone backend, which is only in
  the checkout; the checkout is also what makes the harness editable
  (`agent-workspace/agent_helpers.py`).
- The default phone is `phone-harness config set platform ios|android`;
  `phone-harness config` shows every setting and where it came from;
  `PHONE_HARNESS_PLATFORM=android phone-harness …` overrides for one call.
- `phone-harness --doctor` checks the default phone; `--doctor ios` or
  `--doctor android` checks the other.

Re-run the `phone-harness skill > …/SKILL.md` lines after pulling updates so
the agent's copy matches the code.

## iPhone

- macOS Sequoia+ with **iPhone Mirroring** paired to the phone (open the app
  once and finish its pairing prompts — this needs the physical phone).
- Two permissions for your **terminal**, in System Settings → Privacy & Security:
  - **Accessibility** — taps and keystrokes. Takes effect immediately.
  - **Screen Recording** — seeing the phone. Takes effect after the terminal
    restarts.
  ```bash
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
  ```
- Then `phone-harness --doctor ios`.

> You may need to grant more than these two. They are the permissions we
> *know* are required and all `--doctor` checks; a fresh machine may prompt for
> more the first time an action runs. If `--doctor` passes but taps, typing, or
> capture silently do nothing, look for a macOS permission prompt.

## iPhone over USB (Linux, Windows, macOS)

No mirroring window: the harness talks to the same developer services Xcode
uses — a screenshot service for eyes, a HID service for touches and keys —
through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) over
the cable. Nothing is installed on the phone.

Install **from the checkout**, with the `iphone` extra, on Python 3.13 or
newer. The PyPI release does not have this backend yet, and a
`uv tool install phone-harness` from PyPI would shadow the checkout install
with a `phone-harness` that has no `ios` command.

```bash
cd ~/.phone-harness
uv tool install --python 3.13 --editable ".[iphone]"      # or: python3.13 -m pip install -e ".[iphone]"
sudo apt install usbmuxd                                   # Linux only: the USB device service
# Windows only: install iTunes or the Apple Devices app (they provide the Apple Mobile Device service)
phone-harness ios                                          # must print the phone, not "usage"
```

If `phone-harness ios` prints usage, an older install is first on your PATH:
`which -a phone-harness` shows them; remove the PyPI one
(`uv tool uninstall phone-harness` or `pip uninstall phone-harness` in that
interpreter) and reinstall from the checkout.

- **The phone must run iOS 27 or later.** Older versions report no
  screen-streaming features from the display service and cannot be driven
  this way (checked on iOS 17, 18 and 26.2). `phone-harness ios` shows the
  version it sees.
- Plug the phone in with a data cable and unlock it, then
  `phone-harness ios pair`: tap **Trust** and enter the passcode *on the phone*.
- Turn on **Developer Mode** on the phone: Settings → Privacy & Security →
  Developer Mode (if it is not listed, `phone-harness ios reveal` makes it
  appear). The phone restarts once. Developer Mode lets trusted computers use
  developer services, which is a real loosening of the phone's security —
  say so to the user before they turn it on.
- Each session: `phone-harness ios awake --bg`. It mounts the developer disk
  image if the phone dropped it (it does on every reboot; the first mount
  downloads the image from Apple), opens the USB tunnel and the screen stream
  that authorises input, then waits. `phone-harness ios rest` ends it. Every
  helper needs the session: without it they raise "no CoreDevice session".
- **No cable, over Wi-Fi.** Once, with the phone plugged in:
  `phone-harness ios pair --wifi` (promptless). Then with the phone on the
  same Wi-Fi as the computer: `phone-harness ios awake --connection wifi`
  (or `mirror --connection wifi`). The default `auto` uses the cable when a
  phone is plugged in and Wi-Fi otherwise. The developer image cannot be
  mounted over Wi-Fi, so after a phone reboot plug in once and run
  `phone-harness ios mount`. `--address IP:PORT` dials the phone directly
  when mDNS is blocked (the phone advertises `_remotepairing._tcp`, port
  49152 in testing).
- **Live mirror.** `phone-harness ios mirror` starts the session (USB or
  Wi-Fi) and opens the phone's screen in your browser on 127.0.0.1: click to
  tap, drag to swipe, scroll to scroll, type to type. Safari or Chrome with
  hardware HEVC.
- `phone-harness config set platform ios` (off a Mac, `ios` means this
  backend; on a Mac use `coredevice` to pick it over iPhone Mirroring), then
  `phone-harness --doctor` walks the ladder: Python, library, USB service,
  phone, trust, iOS version, Developer Mode, image, session, screenshot, OCR.
- Coordinates are **screenshot pixels** and the capture is 1:1 with `tap(x, y)`
  — no window offset, no Retina scaling. `ocr()` runs Apple Vision on a Mac
  and RapidOCR (bundled ONNX models) elsewhere.
- pymobiledevice3 is GPL-3.0-or-later. It runs in its own process, the
  session daemon that `ios awake` starts; phone-harness itself stays MIT.

## Android

- adb: `brew install android-platform-tools` (macOS), `apt install adb` (Debian/Ubuntu),
  `winget install Google.PlatformTools` (Windows). Optional: `scrcpy` (`brew` /
  `apt` / `winget install Genymobile.scrcpy`) for a live mirror window during
  `phone-harness android awake`.
- On the phone, once: Settings → About phone → tap **Build number** 7× →
  Settings → System → **Developer options**.
- **USB**: Developer options → **USB debugging** on → plug in → tap **Allow**
  (tick "Always allow from this computer"). Done.
- **Wi‑Fi** (Android 11+, same network as the Mac): Developer options →
  **Wireless debugging** on → tap the row → **Pair device with pairing code** →
  `phone-harness android pair 123456` with the code shown. The phone is
  remembered by name; from then on the harness finds and connects it itself.
- `phone-harness config set platform android` to make it the default, then
  `phone-harness --doctor android`.
- `phone-harness android` shows known phones and what is attached; a plugged-in
  phone always wins over Wi‑Fi. Long task? `phone-harness android awake --bg`
  keeps the phone unlocked for the session without changing any setting;
  `phone-harness android rest` ends it.

## Both

Set up each as above; `phone-harness config set platform …` picks the default,
`PHONE_HARNESS_PLATFORM=…` picks per call. They never interfere — the iPhone
is driven through the mirroring window or the USB tunnel, the Android over adb.

`phone-harness config set telemetry false` turns off anonymous usage telemetry.

## If It Fails

`--doctor` walks the ladder in order and names the missing step. Common ones:

- **iPhone — capture is blank/black**: Screen Recording granted but the
  terminal wasn't restarted; or Mirroring shows an interstitial (iPhone in Use /
  Connect / Mac Locked) — clear it on the Mac, lock the iPhone if it says in use.
- **iPhone — taps do nothing**: Accessibility missing, or another window stole
  focus (helpers re-activate the window; check for a macOS prompt).
- **iPhone — `--doctor` says pyobjc missing on an install that works**: it is
  running a different Python than the one that has pyobjc; use the interpreter
  `pip install -e .` used, or `pip install pyobjc-framework-Quartz
  pyobjc-framework-Vision pyobjc-framework-Cocoa` for that one.
- **iPhone over USB — "no CoreDevice session"**: run `phone-harness ios awake
  --bg`. If awake itself fails, `phone-harness ios` names the rung: not
  plugged in, not trusted (`ios pair`), Developer Mode off, iOS too old.
- **iPhone over USB — awake says the display service is not answering**: the
  phone's media daemon got stuck after an earlier session that was not shut
  down cleanly. awake remounts the developer image once on its own, which
  usually clears it; if it still fails, reboot the iPhone.
- **iPhone over USB — the phone keeps showing the screen-sharing indicator
  and the camera is blocked**: a stream session outlived its computer (cable
  pulled, process killed). Plug the phone in and run `phone-harness ios awake`
  — it ends leftover sessions before starting its own — or `phone-harness ios
  rest`. A phone restart also clears it.
- **iPhone over USB — `locked`**: unlock the phone on the phone. Taps and
  typing refuse while the lock screen is showing; `screenshot()` still works.
- **iPhone over USB — typing a password does nothing**: iOS ignores
  synthesized keystrokes in secure fields. `type_text` without `keystrokes`
  pastes instead, and the phone asks **Allow Paste** the first time an app
  receives a paste from another device; tap it on the phone, or let the agent
  tap it, once per app.
  The phone auto-locks on its own idle timeout; set Auto-Lock to Never for a
  long session if you want (Settings → Display & Brightness).
- **Android — `unauthorized`**: unlock the phone and tap Allow on the "Allow
  USB debugging?" prompt (replug if it does not appear).
- **Android — `no-device`**: USB debugging off, cable/port, or for Wi‑Fi:
  Wireless debugging turned itself off (it does after a reboot) or a different
  network than the Mac. `adb devices` shows what adb sees.
- **Android — `locked`**: unlock the phone; `phone-harness android awake` keeps
  it awake for the session. The harness never types a PIN.
- **Android — the tree is unavailable on some screen**: that screen never goes
  idle (something animates), so `uiautomator` refuses; read `screenshot()`
  instead or move to a screen that settles.
