# Phone Harness 📱

**[phone-harness](https://phone-harness.com)** · let your agent control your phone.

Connect an AI agent — Claude Code, Codex, or any LLM — directly to your real
phone with a thin, editable harness. **iPhone** through the Mac's iPhone
Mirroring window, or **Android** over adb (USB or Wi‑Fi). No jailbreak, no
Xcode, no WebDriverAgent, no app on the phone.

The Mac is the whole transport. iPhone: `screencapture` + Vision-framework OCR
for eyes, HID-level CGEvents for hands. Android: `screencap` for eyes plus the
phone's own accessibility tree — exact text, exact boxes — and `input` for
hands. Nothing between the agent and the phone. The agent writes what's
missing during execution in `agent-workspace/agent_helpers.py`.

```
  ● agent: wants to open Weather
  │
  ● ocr() → "Weather" at (400, 468)
  │
  ● tap(400, 468) → wait_stable() → ocr() confirms the forecast
  ✓ done
```

**Your phone, driven by an agent.**

## Setup prompt

Paste into Claude Code or Codex:

```text
Set up phone-harness for me. Clone https://github.com/ShawnPana/phone-harness into ~/.phone-harness (its canonical home), read `install.md` first, install it so `phone-harness` is a command on my PATH, and register it as an agent skill named phone-harness using `phone-harness skill` as the body, so you reach for it automatically. Then read `SKILL.md` for normal usage, and always read `src/phone_harness/helpers.py` because that is where the functions are. Then read `onboarding.md` and walk me through it.
```

The agent then follows [onboarding.md](onboarding.md): it asks one thing —
which phone is your default, iPhone or Android — and walks you through only
what needs your hands. **iPhone:** pairing iPhone Mirroring once, and granting
the terminal **Accessibility** and **Screen Recording** in System Settings →
Privacy & Security (Screen Recording takes effect after the terminal restarts).
**Android:** turning on Developer options, then either plugging in and tapping
Allow, or Wireless debugging + one 6‑digit pairing code. Then
`phone-harness --doctor` verifies the chain, and `phone-harness config set
platform ios|android` sets the default (both can be set up).

A fresh machine may prompt for more permissions the first time an action runs
— if `--doctor` passes but taps or capture silently do nothing, watch for a
macOS prompt. See [install.md](install.md) for details.

## Why this works

**iPhone.** iPhone Mirroring (macOS Sequoia+) renders the phone as a Mac window
and forwards real mouse and keyboard input as touches. That gives an agent
everything it needs for real-device iOS automation:

- **See** — capture just the mirroring window, OCR it with Apple's Vision
  framework: every visible string with a tap-ready coordinate. The poor man's
  DOM.
- **Act** — CGEvents posted at the HID tap: taps, long-presses, drags/flicks,
  scroll gestures, unicode typing, and the app's own shortcuts (Cmd+1 Home,
  Cmd+2 App Switcher, Cmd+3 Spotlight).
- **Verify** — screenshot again. No DOM means the capture is the ground truth.

Things that do NOT work, learned the hard way: AppleScript `click at` (silently
ignored — the window is a video stream with no accessibility tree), unicode key
payloads (mirroring forwards raw HID keycodes, so typing must use keycodes), a
slow touch-drag (barely moves an iOS list — use wheel scroll for lists, a fast
flick for pages), and input while the window isn't frontmost (swallowed).

**Android.** adb reaches the phone directly, over USB or Wi‑Fi — no window, no
focus, nothing on the Mac has to be in front. `screencap` is the capture; the
phone's own accessibility tree (`uiautomator`) is the text source, so `ocr()`
returns exact strings and boxes and `tap_ui("url_bar")` finds elements OCR
never could; `input tap/swipe/text` are the hands; Back exists. Coordinates
are device pixels, so a screenshot is 1:1 with `tap(x, y)`. The harness finds
the phone itself (a plugged-in one first, else the paired Wi‑Fi phone),
refuses to drive a locked one, and can keep it awake for a session without
touching a setting. Same helpers, same agent code.

## Usage

```bash
./phone-harness <<'PY'
open_app("Notes")
tap_text("New Note")
type_text("hello from the harness")
print([o["text"] for o in ocr()][:10])
PY
```

Day-to-day workflow lives in [SKILL.md](SKILL.md), which [install.md](install.md)
registers as an agent skill (`phone-harness skill` prints the body) so the agent
reaches for it on its own.

## Architecture

- `SKILL.md` — day-to-day usage (the agent-facing product surface)
- `onboarding.md` — the first-run flow the agent walks the user through
- `install.md` — setup reference and troubleshooting
- `src/phone_harness/` — protected core:
  - `transport.py` — the one seam: the op vocabulary every device sits behind,
    and `connect("ios"|"android")`
  - `helpers.py` — the primitives pre-imported into scripts (platform-agnostic)
  - `ios.py`, `mirror.py`, `background.py`, `ocr.py` — the iPhone: window
    discovery, focus, capture, CGEvent input, Vision OCR
  - `android.py` — the Android: adb, the accessibility tree, USB/Wi‑Fi
    resolution and pairing, awake sessions, the `android` CLI
  - `config.py` — settings (`phone-harness config`) and remembered devices
  - `admin.py` — `--doctor`; `run.py` — the CLI (`exec` stdin with helpers in scope)
- `agent-workspace/agent_helpers.py` — helper code the agent edits; auto-loaded
  into every script's namespace

Both transports are stateless per call (window bounds and captures are
re-queried; adb has its own server), so there is no daemon — every invocation
is self-contained. State that must persist (the default platform, paired
Android phones) lives in `~/.config/phone-harness` and `~/.local/state/phone-harness`.

## Development

From a checkout, use `./phone-harness` to run the working tree directly:

```bash
./phone-harness <<'PY'
print(screen_info())
PY
```

## Limits

- iPhone: one phone, one session; unlocking the physical phone pauses mirroring.
  OCR sees text, not semantics — unlabeled icons need a screenshot + a
  vision-capable model.
- Android: the tree costs seconds on a slow phone and is unavailable on screens
  that never go idle (read the screenshot instead); `input text` is ASCII;
  key chords are not a thing over adb; a PIN-locked phone needs the user.
- Both: no multi-touch (no pinch), no camera/Face ID flows, DRM video renders
  black. Connecting the phone is always the user's job.
