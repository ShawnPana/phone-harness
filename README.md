# Phone Harness 📱

**[phone-harness.com](https://phone-harness.com)** · let your agent control your phone.

Connect an AI agent — Claude Code, Codex, or any LLM — directly to your real
iPhone with a thin, editable harness. No jailbreak, no Xcode, no WebDriverAgent.

The Mac's iPhone Mirroring window is the whole transport: `screencapture` +
Vision-framework OCR for eyes, HID-level CGEvents for hands. Nothing between the
agent and the phone. The agent writes what's missing during execution in
`agent-workspace/agent_helpers.py`.

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
Set up phone-harness for me. Clone https://github.com/ShawnPana/phone-harness
into ~/.phone-harness (its canonical home) and read `install.md` first to install
it and connect it to my real iPhone
through the macOS iPhone Mirroring app — install it so `phone-harness` is a
command on my PATH, and register it as an agent skill named phone-harness using
`phone-harness skill` as the body, so you reach for it automatically. Then read
`SKILL.md` for normal usage, and always read `src/phone_harness/helpers.py`
because that is where the functions are. Whenever you capture or verify the
screen, activate the iPhone Mirroring window so I can see what you're doing on
the phone.

Setup needs two things only I can do: pairing iPhone Mirroring with my phone
once, and granting the terminal Accessibility + Screen Recording in System
Settings — walk me through those and wait for me. Verify with
`./phone-harness --doctor`.

After it's installed, as a quick demo that interaction works, go to my Home
Screen and — if the phone is connected and unlocked — ask me whether you should
open the Weather app as a harmless test; only open it if I say yes. If the
session is paused or the phone is locked, just tell me the doctor status instead.
```

The agent will walk you through the two things only you can do: **pairing**
iPhone Mirroring with your phone once (the pairing prompts need the physical
phone), and granting the terminal **Accessibility** (taps & keystrokes) and
**Screen Recording** (seeing the phone) in System Settings → Privacy &
Security. Screen Recording takes effect after the terminal restarts;
Accessibility is immediate. Then `./phone-harness --doctor` verifies the whole
chain.

These are the permissions currently known to be required. A fresh machine may
prompt for more the first time an action runs — if `--doctor` passes but taps or
capture silently do nothing, watch for a macOS permission prompt. See
[install.md](install.md) for details.

## Why this works

iPhone Mirroring (macOS Sequoia+) renders the phone as a Mac window and forwards
real mouse and keyboard input as touches. That gives an agent everything it
needs for real-device iOS automation:

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
- `install.md` — permissions bootstrap and troubleshooting
- `src/phone_harness/` — protected core (~500 lines):
  - `mirror.py` — window discovery, focus, capture, CGEvent input
  - `ocr.py` — Vision-framework text recognition → screen-point boxes
  - `helpers.py` — the primitives pre-imported into scripts
  - `admin.py` — `--doctor`
  - `run.py` — the CLI (`exec` stdin with helpers in scope)
- `agent-workspace/agent_helpers.py` — helper code the agent edits; auto-loaded
  into every script's namespace

The mirror transport is stateless (window bounds and captures are re-queried per
call), so there is no daemon — every invocation is self-contained.

## Development

From a checkout, use `./phone-harness` to run the working tree directly:

```bash
./phone-harness <<'PY'
print(screen_info())
PY
```

## Limits

- One phone, one session; unlocking the physical phone pauses mirroring.
- No multi-touch (no pinch), no camera/Face ID flows, DRM video renders black.
- OCR sees text, not semantics — unlabeled icons need a screenshot + a
  vision-capable model.
