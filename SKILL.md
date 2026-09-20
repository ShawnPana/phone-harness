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
- Start every script with two comment lines: `# task:` restating the user's
  request in one sentence, and `# step:` saying what this particular script
  does toward it. Keep the `# task:` line identical across all scripts for the
  same request.

  ```python
  # task: report the iOS version and model name from Settings
  # step: scroll to General, open About, read the screen
  ```
- **Tell the user what you are doing as you go.** A phone task is many short
  scripts, and the user sees none of them: say one line before each script
  (what you are about to do) and one line after it (what you saw). Never run
  two scripts in a row in silence. The `# task:` / `# step:` comments are not
  this — the user cannot see them.
- Helpers are pre-imported. All coordinates are global screen points.
- `ensure_mirroring()` launches the window and gates on connection. The
  default build works the phone **without taking the user's focus**: capture is
  by window id and taps and keystrokes are event records delivered straight to
  the app. Scrolling is the exception — macOS routes a scroll to whichever
  window sits under the pointer, so a scroll raises the mirroring window for
  the length of the gesture and hands focus straight back. Expect a brief
  flicker on scrolls and nothing on anything else.
  `PHONE_HARNESS_BACKGROUND=0` forces the classic path, which focuses before
  every action.

## Screen Workflow

- Prefer `ocr()` over eyeballing screenshots: every visible string comes back
  with a tap-ready center point — `[{text, confidence, x, y, w, h}]`. Filter
  in Python before printing.
- Tap by label: `tap_text("Weather")`. On failure it raises with what IS
  visible, so read the exception before retrying.
- Icons without labels: `screenshot()`, view the image, and use
  `tap_image_point(x, y, image_size=...)` with coordinates measured in the
  screenshot. Do **not** pass screenshot pixel coordinates directly to `tap()`:
  `tap()` expects global macOS screen points. If using `tap()` instead, first
  convert with `image_point()` using the current `screen_info()`; never estimate
  the window offset manually.
- **Work in a loop: act, verify, adapt.** There is no DOM to assert against
  and no return value that means "it worked", so the loop is the method:

  1. **Name what should change** before you act — a title, a row, a username,
     a field's contents. If you cannot name it, you cannot tell success from a
     no-op, and most phone failures are silent no-ops.
  2. **Do one action**, then check that one thing. How you check is yours:
     `ocr()` is cheap and gives every visible string with a tap-ready point;
     `screenshot()` costs more but shows you everything OCR cannot read —
     icons, images, whether a row is highlighted. Use the cheap one in a loop
     and look at an image when you are stuck or when the answer is visual.
  3. **Once a sequence is proven, batch it** — a whole sub-task in one
     invocation is much faster than a call per turn. Batch what you have
     already watched work, and keep one cheap check at the end.
  4. **When a check fails, isolate.** Re-run that single action on its own,
     look at the screen, form one guess about why, test the guess, and adapt.
     Do not re-run the whole batch hoping it lands.
  5. **Keep what you learn**: put reusable checks and fixed-up steps in
     `agent-workspace/agent_helpers.py` so the next task starts ahead.

- **The harness reports, you decide.** Helpers hand back observations — text,
  coordinates, what was on screen before and after — and never a verdict on
  whether your intent was achieved. Only you know what you were after, so judge
  from the content you expected.

  `ocr()` and `screenshot()` are the observation surface, and what you do with
  them is entirely your call: diff two OCR sets, watch one label, count rows,
  compare a crop, poll until something appears. The harness deliberately does
  not pick a comparison for you — it tried, and every rule that fit a list
  broke on a feed, and every rule that fit a feed broke on a strip that scrolls
  inside a still screen. Write the check that matches what you asked for, and
  put it in `agent_helpers.py` when it turns out to be reusable.
- Navigation: `home()`, `app_switcher()`, `open_app("Notes")` (Spotlight),
  `scroll("down")`, `swipe("up")`, `type_text("...")`, `press("return")`,
  `long_press(x, y)`.
- **Directions: `scroll` says what you want to SEE, `swipe` says which way the
  finger goes.** They disagree on purpose, because English does — "scroll down
  the page" and "swipe up for the next video" describe the same outcome.

  ```
  scroll("down")   show me what is further down
  swipe("up")      thumb up  (the phrasing everyone uses for "next")
  ```

  `scroll`, `scroll_screen`, `scroll_until` and `scroll_collect` all take the
  content-direction; only `swipe` takes finger motion. `"left"`/`"right"` work
  on both.

  **Use `scroll` for anything scrollable.** On macOS 26 a vertical touch-drag is
  dropped, so `swipe("up")`/`swipe("down")` move nothing in a list or a feed --
  measured on Settings and on TikTok. Horizontal still works, so `swipe("left")`
  / `swipe("right")` remain the way to flip Home Screen pages and carousels,
  which a scroll cannot do.

  **Breaking change for `scroll`:** it used to take finger motion too, so the
  old `scroll("up")` is today's `scroll("down")`. `swipe` is unchanged.
- **Scrolling**: `scroll(direction, amount, at=...)` for one gesture;
  `scroll_until(done)` to stop when your predicate on the visible OCR is met;
  `scroll_collect(extract, key=...)` to walk a list, de-duping as it goes.
  `scroll_until` stops on your predicate; `scroll_collect` stops when your
  extractor stops finding new items and returns `{items, stop, scrolls}` with
  `stop` of `'reached-end'` or `'max-scrolls'`. Both end on YOUR check, so an
  extractor that misses rows will end the walk early — make it robust before
  blaming the scroll.
  `scroll_screen()` is the single-step primitive and returns what is on screen
  (`before`, `after`, `boxes`); what counts as a successful scroll
  is yours to decide, because it differs per app — a list translates, a feed
  swaps to the next item, an inner strip moves while the rest of the screen
  holds still. To see what happened, take a `screenshot()` and look at it.
  `at` aims the gesture. Only the scroll view under that point moves, so pass
  it whenever the thing you want to scroll is not the full-screen list.
- Raw Quartz is the escape hatch: `import Quartz` in your script for anything
  the helpers don't cover — but raw CGEvents don't ride the helpers' delivery
  path, and where they land is its own question per event type. Check what
  actually happened on screen rather than assuming the event arrived.

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
- **adb is the native language here, and it is first-class.** `shell(cmd)`
  runs `adb shell cmd` on whichever phone the harness chose, so anything you
  know how to do with adb, do: `shell("input tap 360 640")`,
  `shell("input keyevent KEYCODE_BACK")`, `shell("am start -n pkg/.Activity")`,
  `shell("dumpsys notification --noredact")`, `shell("pm list packages -3")`.
  The input helpers (`tap`, `swipe`, `press`, `home`) are one-line wrappers
  over the same commands — use whichever you think in. What the harness adds
  that raw adb does not: finding and reconnecting the phone, and reading the
  screen as a short list instead of a page of XML.
- **On Android, `scroll` and `swipe` are different gestures.** `scroll` moves
  the content and stops: no momentum, the same distance every time, so use it
  (and `scroll_until` / `scroll_collect`) to walk a list without skipping
  rows. `swipe` is a flick and coasts past whatever was next — right for "next
  video" or changing pages, wrong for reading a list. (The note above about
  vertical swipes doing nothing is about iPhone Mirroring; here both work.)
- `open_app("TikTok")` takes the name a person would say, a package id, or a
  fragment of one, and returns the package it launched. When nothing matches,
  the error lists what is installed. `back()`, `current_app()`, `list_apps()`
  exist.
- `press()` takes single keys only (`"enter"`, `"back"`, `"tab"`); chords
  raise Unsupported. `type_text` needs a focused field, same as iOS, and types
  ASCII: adb cannot type emoji or accented letters.
- **Some screens never give up their tree** — a playing video, some Settings
  pages. On a Mac, `ocr()` then reads the screenshot with Vision instead
  (`source: "pixels"`, fuzzier, still tap-ready; the first read costs ~12s
  while uiautomator gives up, later reads are fast). Elsewhere it raises
  saying so; take a `screenshot()` and look at it instead of retrying.
  `ui()` / `tap_ui()` need the real tree and keep raising on such screens.
- No focus to keep: nothing on the Mac has to be frontmost, and
  `interruption(before, after)` always reports nothing disturbed.
- **Verify cheaply, then read.** adb reports nothing about outcomes — a tap on
  empty space "succeeds". After an action: `wait_for_app("com.android.chrome")`
  (~0.1s per poll) or `wait_for_text("Got it")` (returns the box or None),
  then `ui()`/`ocr()` once for contents. The tree costs ~2-3s a call on a slow
  phone and a screenshot ~0.5s, so batching a whole sub-task in one invocation
  is worth a lot — but batch the steps you have already watched work, and keep
  a check at the end. A batch of unverified steps fails silently and tells you
  nothing about which one broke.
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

## Cloud phones

`phone-harness cloud start` rents the user's own Android phone from Phone
Harness Cloud and connects to it; from then on every script drives that phone
with nothing exported, exactly as the Android section describes. It is the
same phone each time: apps, logins and normally the exact screen are kept
between sessions.

```bash
phone-harness cloud            # signed in? a phone attached? minutes left?
phone-harness cloud start      # about 15s; safe to run twice, it reattaches
phone-harness cloud watch      # (re)open the live view; `start` already opens it
phone-harness cloud stop       # ends billing and saves the phone
```

- **It bills by the minute while it is up.** Start it once and keep it for the
  whole conversation — a stop and a restart between two requests wastes more
  than it saves. Stop it when the user is done, and say that you did.
- **Stop before the deadline, because a session cannot be extended.** One
  that simply runs out keeps the phone's data but not its running state. The
  harness warns on stderr when under two minutes remain; `phone-harness cloud`
  shows the time left. If the task needs longer, stop and start again: the
  phone comes back where it was in about 15 seconds. `cloud stop` returns at
  once; the save finishes on its own, and a `cloud start` during it waits.
- **A session the user started is theirs.** `cloud start` attaches to a phone
  that is already running instead of starting another; leave that one running
  unless they ask you to stop it.
- `Not signed in` means the user has to run `phone-harness cloud login` and
  approve it in a browser. Relay that; you cannot do it for them.
- `cloud start` opens the phone's live view in the user's browser, so they
  can watch you work; `cloud watch` reopens it if they closed it. That view
  is read-only. When the user wants to take the controls themselves — to
  type a password, pass a 2FA prompt, or just drive — run
  `phone-harness cloud open`: the dashboard, behind their own sign-in, with
  the interactive viewer. Wait for them to say they are done before you
  touch the phone again. `cloud start --temp` is a throwaway phone that
  keeps nothing.
- The connection is handled for you, including reconnecting after a drop.
  The adb address the CLI shows is not a secret; the unlock code is, and you
  never need it — do not look for it, print it, or ask the user for it.

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
