# phone-harness install

phone-harness drives a real phone. First-run flow for agents:
https://github.com/ShawnPana/phone-harness/blob/main/onboarding.md.
Day-to-day usage is `phone-harness skill`. It works with an **iPhone**
through the macOS iPhone Mirroring app, an **Android** over adb (USB or
Wi‑Fi), or a phone from Phone Harness Cloud — an Android over adb, or an
iPhone the helpers drive over HTTPS (`phone-harness cloud start` with that
phone's name from `cloud ls`). Same helpers either way; you choose a
default and can switch per call. Reading on-screen text is `screen()`;
the helper names and signatures are in the skill (`SKILL.md`, printed by
`phone-harness skill`).

## Fast Path

```bash
uv tool install --python 3.12 --upgrade --force phone-harness
```

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12. The package itself
accepts Python 3.10 or newer. **iPhone Mirroring needs a Mac.** A cloud
iPhone (`cloud start` of the iOS catalog entry) is driven over HTTPS from
any OS. **Android works on macOS, Linux and Windows** and is the default
off a Mac.

Register the skill so the agent reaches for it:

```bash
# Codex
mkdir -p ${CODEX_HOME:-$HOME/.codex}/skills/phone-harness && phone-harness skill > ${CODEX_HOME:-$HOME/.codex}/skills/phone-harness/SKILL.md
```

Claude Code and other agents: register a skill named phone-harness with
`phone-harness skill` as the body plus a short trigger sentence (for
example, "Use this when the task needs the user's phone.").

Quick check:

```bash
phone-harness <<'PY'
ensure_device()
PY
```

That reaches the default phone. If it fails, use the sections below.

- Helpers you add live in `~/.phone-harness/agent-workspace`, outside the
  install, so an upgrade does not wipe them. When `~/.phone-harness` is a
  git checkout, they live in
  `~/.local/share/phone-harness/agent-workspace` (`XDG_DATA_HOME` if that
  is set). The same paths are used on every OS. `PH_AGENT_WORKSPACE`
  overrides these. The first run copies the packaged defaults when that
  directory does not exist. An existing workspace is left untouched,
  including a custom `agent_helpers.py`. To get a fresh default, delete
  the workspace folder; it is recreated on the next run.
- The default phone is `phone-harness config set platform ios|android`;
  `phone-harness config` shows every setting and where it came from;
  `PHONE_HARNESS_PLATFORM=android phone-harness …` overrides for one call.
- `phone-harness doctor` (and `--doctor`) checks the phone the helpers
  would drive. With no platform, an attached cloud iPhone is checked over
  its control URL (`screen.text`, `screen.capture`, `screen.bounds`) and
  adb is not required. `doctor android`, `--doctor ios`, and
  `PHONE_HARNESS_PLATFORM` still check that local phone.
- Re-run the skill registration after `phone-harness update` so the
  agent's copy matches the code. `phone-harness update` does the upgrade:
  `uv tool upgrade phone-harness` when this is a uv tool, `git pull
  --ff-only` in a checkout, otherwise it prints `pip install -U
  phone-harness` and does not run it. It never updates on its own. A
  newer PyPI release is one stderr line, at most once a day, and only on
  a terminal; `PHONE_HARNESS_NO_UPDATE_CHECK=1` skips the check.
- **No phone on your desk?** `phone-harness cloud login`, then
  `phone-harness cloud ls` and `phone-harness cloud start` (the account
  default) or `phone-harness cloud start PHONE` for a catalog id, label, or
  `ios` / `android` when that names one phone. An Android needs adb, below.
  An iPhone needs no adb and no Mac; driving it is **Cloud iPhone** in
  the skill. A start lasts 15 minutes unless you pass `--for` (`90s`, `15m`,
  or a number of minutes). New accounts get $5 of credit (100 minutes).

No uv? `pip install phone-harness` works too. Upgrade that install with
`pip install -U phone-harness` (what `phone-harness update` prints for it).

## Working from a checkout (contributors)

```bash
git clone https://github.com/ShawnPana/phone-harness ~/.phone-harness
cd ~/.phone-harness
pip install -e .
```

`pip install -e .` pulls pyobjc on macOS only. Register the skill the same
way as the Fast Path. A checkout uses its own `agent-workspace/`. Because
that clone sits at `~/.phone-harness`, a separate pip or uv install stores
helpers in `~/.local/share/phone-harness/agent-workspace` instead of
writing into the checkout.

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
`PHONE_HARNESS_PLATFORM=…` picks per call. The desk phones never interfere —
the iPhone is driven through the mirroring window, the Android over adb. A
cloud iPhone is a third path: HTTPS ops, covered in the skill.

`phone-harness config set telemetry false` turns off anonymous usage telemetry.

## If It Fails

`--doctor` walks the ladder in order and names the missing step. Common ones:

- **iPhone — capture is blank/black**: Screen Recording granted but the
  terminal wasn't restarted; or Mirroring shows an interstitial (iPhone in Use /
  Connect / Mac Locked) — clear it on the Mac, lock the iPhone if it says in use.
- **iPhone — taps do nothing**: Accessibility missing, or another window stole
  focus (helpers re-activate the window; check for a macOS prompt).
- **iPhone — `--doctor` says pyobjc missing on an install that works**: it is
  running a different Python than the one that has pyobjc. Re-run the Fast
  Path command, or use the interpreter `pip install -e .` used in a checkout.
  Or `pip install pyobjc-framework-Quartz pyobjc-framework-Vision
  pyobjc-framework-Cocoa` for that interpreter.
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
