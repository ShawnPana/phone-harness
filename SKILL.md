---
name: phone-harness
description: "Control the user's phone — iPhone through the Mac's iPhone Mirroring window, or an Android over adb: open apps, tap, type, swipe, read the screen."
---

# phone-harness

Direct control of the user's phone. iPhone: through the iPhone Mirroring app —
screenshots + Vision OCR for eyes, HID-level CGEvents for hands. Android: over
adb — screenshots + the phone's accessibility tree for eyes, `input` for hands
(see the Android section; the helpers are the same). `phone-harness config`
shows which is the default. For task-specific edits, use
`agent-workspace/agent_helpers.py`. For setup or permission problems, read
`install.md`.

## When Not to Use

If the task is doable on the Mac or the web — a website, an API, an app with a
web equivalent — do it there and leave the phone alone. Use phone-harness only
when the task genuinely needs the phone: iOS-only apps, things tied to the
user's phone number or 2FA, testing how something looks on the phone.

## Usage

```bash
phone-harness <<'PY'
print(screen_info())
PY
```

- Invoke as `phone-harness`. Use heredocs for multi-line commands.
- Helpers are pre-imported. All coordinates are global screen points.
- `ensure_mirroring()` launches the window and gates on connection. The
  default build then works the phone **without focusing it**: capture is by
  window id and input is an event record delivered straight to the app, so a
  task never steals the user's screen. `PHONE_HARNESS_BACKGROUND=0` forces the
  classic path, which must focus before every action.

## Screen Workflow

- Prefer `ocr()` over eyeballing screenshots: every visible string comes back
  with a tap-ready center point — `[{text, confidence, x, y, w, h}]`. Filter
  in Python before printing.
- Tap by label: `tap_text("Weather")`. On failure it raises with what IS
  visible, so read the exception before retrying.
- Icons without labels: `screenshot()`, view the image, compute the point
  (image px ÷ scale + window origin — `screen_info()` has both sizes), then
  `tap(x, y)`.
- **Verify after every action**: `wait_stable()` then `ocr()`/`screenshot()`.
  There is no DOM to assert against; the capture is the ground truth.
- Navigation: `home()`, `app_switcher()`, `open_app("Notes")` (Spotlight),
  `swipe("up")`, `scroll()`, `type_text("...")`, `press("return")`,
  `long_press(x, y)`.
- **Scrolling a list**: use `scroll_collect(extract, key=...)` to walk a list
  to its true end, de-duping as it goes — it returns `{items, stop, scrolls}`
  where `stop` is `'reached-end'` or `'max-scrolls'`. Use `scroll_until(done)`
  to stop when a predicate on the visible OCR is met. Both decide "done" from
  whether the **screen actually moved**, not from whether your parser found
  new rows — a dense screen or a missed OCR line will not end the scroll
  early. Each step settles first so lazy-loaded content arrives before the
  movement check. `scroll_screen()` is the single-step primitive if you need
  it. In the background build these scroll with momentum flicks — wheel
  events only route to a *focused* window, and a slow touch-drag barely moves
  an iOS list before bouncing back; only a fast flick carries it.
- Raw Quartz is the escape hatch: `import Quartz` in your script for anything
  the helpers don't cover — but raw CGEvents don't ride the helpers' delivery
  path. They land only while the window is frontmost and are swallowed
  silently otherwise, scroll-wheel events included: `activate()` first, then
  verify the screen actually changed.

## Android

Same helpers, different phone. `phone-harness config set platform android`
makes Android the default (`phone-harness config` shows every setting and
where it came from); until then, or to override per call, prefix with
`PHONE_HARNESS_PLATFORM=android`. The harness
finds the phone itself — a USB phone if plugged in, else the paired Wi-Fi
phone — so there is nothing to select.

```bash
PHONE_HARNESS_PLATFORM=android phone-harness <<'PY'
open_app("chrome"); wait_stable()
tap_ui("Got it")                    # exact label from the accessibility tree
PY
```

- Coordinates are device pixels; the screenshot is 1:1 with `tap(x, y)`.
- `ocr()` is the accessibility tree (`source: "tree"`) — exact, no misreads.
  Prefer `ui()` / `find_nodes()` / `tap_ui()`: they also see elements with no
  visible text (icons with a content-description, fields by resource-id like
  `tap_ui("url_bar")`). `ocr_pixels()` is Unsupported here.
- `back()`, `current_app()`, `list_apps()` exist. `open_app("chrome")`
  matches installed package ids and returns the one launched.
- `press()` takes single keys only (`"enter"`, `"back"`, `"tab"`); chords
  raise Unsupported. `type_text` needs a focused field, same as iOS.
- No focus to keep: nothing on the Mac has to be frontmost, and
  `interruption(before, after)` always reports nothing disturbed.
- **Verify cheaply, then read.** adb reports nothing about outcomes — a tap on
  empty space "succeeds". After an action: `wait_for_app("com.android.chrome")`
  (~0.1s per poll) or `wait_for_text("Got it")` (returns the box or None),
  then `ui()`/`ocr()` once for contents. The tree costs ~2-3s a call on a slow
  phone and a screenshot ~0.5s, so batch a whole sub-task in one invocation
  and filter in Python rather than one call per turn.
- **The phone locks itself** after its screen timeout. `connection_state()`
  reports `locked`; taps and `ocr()` refuse with the same message. Ask the
  user to unlock — never type a PIN. `screenshot()` still works locked, so you
  can show them what you see. For a task longer than a minute, ask the user,
  then run `phone-harness android awake --bg`: it keeps the phone awake for
  the session (and opens a mirror window if scrcpy is installed) without
  changing any phone setting; `phone-harness android rest` ends it and lets
  the phone sleep. Do that at the end of the task.
- Connection is still the user's job (USB debugging + Allow, or Wireless
  debugging + `phone-harness android pair CODE`); on `no-device` the
  error names the missing step — relay it, don't retry-loop.
  `phone-harness android` shows known phones and what is attached.

## Consent

This is the user's real phone. Stop and ask before anything outward-facing or
hard to reverse: sending a message, posting, purchasing, deleting, changing
settings.

## Connection is the user's job

The harness never connects the phone for you. Connecting or resuming mirroring
is a physical action — opening the app, approving the prompt, and (crucially)
**locking the iPhone when it says "iPhone in Use"** — that only the user can do.

`ensure_mirroring()` gates every task on this: if the phone isn't connected it
raises a clear message (call `connection_state()` yourself to check —
`ready` / `blocked` / `no-window` / `not-running`). When you hit that:

- **STOP and relay the message. Ask the user to connect the phone themselves.**
- **Never** tap `Connect` / `Continue`, and **never** loop-poll waiting for the
  connection. Tapping Connect while the phone is unlocked does nothing, and
  polling just burns time — the only fix is the user locking/connecting the
  phone. Retry once *after they confirm they've done it*, not before.

## Gotchas

- **Unfocused input is swallowed silently — for events you post yourself.**
  The helpers are immune in the background build (input goes straight to the
  app), but raw CGEvents and the `PHONE_HARNESS_BACKGROUND=0` path need the
  window frontmost: `activate()` before posting, and re-activate if a click
  steals focus mid-task. The failure looks exactly like "scrolling is broken"
  or "the list already ended" — when a gesture changes nothing on screen,
  check focus before inventing another theory.
- **The window is a video stream.** macOS accessibility sees nothing inside
  it; AppleScript `click at` fails silently. Only HID-level CGEvents work.
- **The window moves.** Never cache coordinates across calls; `ocr()` and
  `swipe()` re-query bounds every time.
- **Unlocking the physical phone pauses the session** ("iPhone in Use"). Do not
  tap through the resume screen — stop and ask the user to lock/connect the
  phone (see "Connection is the user's job").
- **`type_text` needs an iOS text field focused first** — tap the field, wait
  for the keyboard, then type. It fails *silently* when nothing is focused: the
  text goes to whatever is focused instead, or nowhere. Verify with a capture,
  and if a tap will not take focus, `press("tab")` moves between fields.
- **`type_text` pastes; it does not type.** That is deliberate — the keystroke
  path runs through iOS autocorrect, which rewrites words as they land ("Thu"
  becomes "thru"). Pass `keystrokes=True` for fields that need real key events.
  The typed text stays on the Mac clipboard afterwards (restoring the old
  clipboard raced the phone and could paste it instead).
- **Home-Screen labels are not tap targets.** `tap_text("Weather")` hits the
  label and nothing happens; the icon is ~35 points above it. Use
  `tap_icon("Weather")` (agent helper) on the Home Screen; `tap_text` works
  fine for in-app buttons and list rows.
- Mouse taps map to touches 1:1, but there is no multi-touch: no pinch, no
  two-finger gestures.
