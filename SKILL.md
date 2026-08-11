---
name: phone-harness
description: "Control the user's iPhone through the Mac's iPhone Mirroring window: open apps, tap, type, swipe, read the screen."
---

# phone-harness

Direct iPhone control via the iPhone Mirroring app — screenshots + Vision OCR
for eyes, HID-level CGEvents for hands. For task-specific edits, use
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
- `ensure_mirroring()` launches and focuses the window; input helpers focus it
  automatically before posting events.

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
  it. These use wheel scrolling (a slow touch-drag barely moves an iOS list
  and bounces back).
- Raw Quartz is the escape hatch: `import Quartz` in your script for anything
  the helpers don't cover.

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

- **Unfocused input is swallowed silently.** The window must be frontmost;
  helpers call `activate()` but if a click steals focus mid-task, re-activate.
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
- **Home-Screen labels are not tap targets.** `tap_text("Weather")` hits the
  label and nothing happens; the icon is ~35 points above it. Use
  `tap_icon("Weather")` (agent helper) on the Home Screen; `tap_text` works
  fine for in-app buttons and list rows.
- Mouse taps map to touches 1:1, but there is no multi-touch: no pinch, no
  two-finger gestures.
