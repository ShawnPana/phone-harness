# phone-harness install

phone-harness drives a real phone from your computer (first-run flow for agents:
`onboarding.md`; day-to-day usage: `SKILL.md`). It works with an **iPhone** through the macOS
iPhone Mirroring app (Mac only), or an **Android** over adb (USB or Wi‑Fi, from
macOS, Windows or Linux). Same helpers either way; you choose a default and can
switch per call.

## Common

```bash
git clone https://github.com/ShawnPana/phone-harness ~/.phone-harness   # canonical home
cd ~/.phone-harness
pip install -e .                      # the global `phone-harness` command (pulls pyobjc)

# register as an agent skill so Claude Code / Codex reach for it automatically
mkdir -p ~/.claude/skills/phone-harness
phone-harness skill > ~/.claude/skills/phone-harness/SKILL.md
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/phone-harness"
phone-harness skill > "${CODEX_HOME:-$HOME/.codex}/skills/phone-harness/SKILL.md"
```

On Windows, in PowerShell (`${VAR:-default}` is bash syntax — it expands to
nothing here and the file lands in the wrong place, silently):

```powershell
git clone https://github.com/ShawnPana/phone-harness $HOME\.phone-harness
cd $HOME\.phone-harness
pip install -e .

$claude = "$HOME\.claude\skills\phone-harness"
New-Item -ItemType Directory -Force -Path $claude | Out-Null
phone-harness skill | Out-File -Encoding utf8 "$claude\SKILL.md"

$codex = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "$HOME\.codex" }
New-Item -ItemType Directory -Force -Path "$codex\skills\phone-harness" | Out-Null
phone-harness skill | Out-File -Encoding utf8 "$codex\skills\phone-harness\SKILL.md"
```

- Python 3.10+. Only the CLI? `pip install phone-harness` works too; the
  checkout is what makes the harness editable (`agent-workspace/agent_helpers.py`).
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

## Android

- adb: `brew install android-platform-tools` (macOS),
  `winget install Google.PlatformTools` (Windows),
  `sudo apt install android-tools-adb` (Linux). Optional, for a live mirror
  window during `phone-harness android awake`: `brew install scrcpy` /
  `winget install Genymobile.scrcpy` / `sudo apt install scrcpy`.
  winget puts both on PATH but only for **newly started** shells — restart the
  terminal, or skip PATH entirely with
  `phone-harness config set android.adb C:\path\to\adb.exe` (same for
  `android.scrcpy`).
- On the phone, once: Settings → About phone → **Software information** → tap
  **Build number** 7× → back out to Settings → **Developer options** (on
  Samsung/One UI it appears at the bottom of the main Settings list). Enabling
  Developer options is a separate step from **USB debugging** below, and
  TalkBack's Accessibility → *Developer settings* is an unrelated menu.
- **USB**: Developer options → **USB debugging** on → plug in → tap **Allow**
  (tick "Always allow from this computer"). Done.
- **Wi‑Fi** (Android 11+, same network as this computer): Developer options →
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
`PHONE_HARNESS_PLATFORM=…` picks per call. The two never interfere — the
iPhone is driven through the mirroring window, the Android over adb.

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
- **Android — `unauthorized`**: unlock the phone and tap Allow on the "Allow
  USB debugging?" prompt (replug if it does not appear).
- **Android — `no-device`**: USB debugging off, cable/port, or for Wi‑Fi:
  Wireless debugging turned itself off (it does after a reboot) or a different
  network than this computer. `adb devices` shows what adb sees.
- **Android — `adb devices` is empty over USB on Windows**: the phone can be
  fully enumerated (it shows up as an MTP drive) and still expose no adb
  interface, because USB debugging is off — that toggle, not Developer options,
  is what adds it. Check with
  `Get-PnpDevice -PresentOnly | ? FriendlyName -match 'ADB'`; if that is empty,
  turn on USB debugging. If it lists a device with a problem instead, install
  the OEM driver (Samsung: developer.samsung.com/android-usb-driver).
- **Android — `adb connect <IP>` says "actively refused"**: `adb connect`
  defaults to port 5555, which nothing listens on. Android 11+ Wireless
  debugging uses a random port shown in its dialog — pair first, or pass the
  full `IP:PORT`. Port 5555 only opens after `adb tcpip 5555`, which itself
  needs a working USB connection.
- **Android — pairing finds no phone**: `_adb-tls-pairing` is advertised only
  while the "Pair device with pairing code" dialog is open on the phone. Keep it
  open, and if the network blocks mDNS use
  `phone-harness android pair IP:PORT CODE` with both values from that dialog.
- **Android — `locked`**: unlock the phone; `phone-harness android awake` keeps
  it awake for the session. The harness never types a PIN.
- **Android — the tree is unavailable on some screen**: that screen never goes
  idle (something animates), so `uiautomator` refuses; read `screenshot()`
  instead or move to a screen that settles.
