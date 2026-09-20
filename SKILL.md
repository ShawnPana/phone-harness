---
name: phone-harness
description: "Control the user's phone — an iPhone through the Mac's iPhone Mirroring window, an Android over adb, or a rented cloud Android: open apps, tap, type, swipe, read the screen."
---

# phone-harness

Direct control of a phone from Python scripts. The same helpers drive three
kinds of phone; what differs is how each one sees and touches the screen, and
that is what the per-phone sections below are for. Read **Which phone**, the
shared **Working method**, and then only the section for the phone in front
of you.

## Which phone?

| Phone | How it is reached | Eyes | Hands | Read |
| --- | --- | --- | --- | --- |
| **Cloud Android** — rented from Phone Harness Cloud, the user's own saved phone | `phone-harness cloud start` connects it; nothing to export | accessibility tree (exact), Vision OCR fallback on a Mac | adb `input` | Working method, then **Cloud phones** and **Android** |
| **Android on the desk** — USB or paired Wi-Fi | the harness finds it | accessibility tree (exact), Vision OCR fallback on a Mac | adb `input` | Working method, then **Android** |
| **iPhone** — through the Mac's iPhone Mirroring window | the user connects it | screenshots + Vision OCR | HID-level CGEvents into the window | Working method, then **iPhone** |

`phone-harness config` shows the default platform (`ios` on a Mac, `android`
elsewhere) and every other setting. `phone-harness cloud` shows whether a cloud
phone is attached; while one is, scripts drive it regardless of the default.
`PHONE_HARNESS_PLATFORM=android` overrides per call.

**When not to use any of them:** if the task is doable on the Mac or the web —
a website, an API, an app with a web equivalent — do it there and leave the
phone alone. Use a phone when the task genuinely needs one: phone-only apps,
things tied to a phone number or 2FA, checking how something looks on a phone.

## Working method (every phone)

```bash
phone-harness <<'PY'
# task: report the OS version and model name from Settings
# step: open Settings, find About, read the screen
open_app("Settings")
print([o["text"] for o in ocr()][:10])
PY
```

- Invoke as `phone-harness` with a heredoc. Helpers are pre-imported. Start
  every script with `# task:` (the user's request in one sentence, identical
  across the scripts of one request) and `# step:` (what this script does).
- **Tell the user what you are doing as you go.** A phone task is many short
  scripts and the user sees none of them: one line before each script (what
  you are about to do), one line after (what you saw). Never run two scripts
  in a row in silence. The `# task:`/`# step:` comments are not this — the user
  cannot see them.
- **Read with `ocr()`, not by eyeballing screenshots.** Every visible string
  comes back with a tap-ready centre: `[{text, confidence, source, x, y, w, h}]`.
  Filter in Python before printing. `tap_text("Weather")` taps by label and,
  on failure, raises with what IS visible — read the exception before retrying.
  `screenshot()` costs more but shows what OCR cannot: icons, images, state.
- **Act, verify, adapt.** There is no DOM to assert against and no return value
  that means "it worked", so:
  1. Name what should change before you act — a title, a row, a field's
     contents. Most phone failures are silent no-ops; if you cannot name the
     expected change you cannot tell success from one.
  2. Do one thing, then check that one thing: `wait_for_text("Got it")`,
     `wait_for_app("com.android.chrome")` or `wait_stable()`, then one `ocr()`.
     Do not `wait(n)` for a screen to load.
  3. Once a sequence is proven, batch it: a whole sub-task in one script is far
     faster than a call per turn. Keep one cheap check at the end.
  4. When a check fails, isolate: re-run that one action, look at the screen,
     form one guess, test it. Do not re-run the batch hoping it lands.
  5. Keep what you learn in `agent-workspace/agent_helpers.py`.
- **The harness reports, you decide.** Helpers return observations, never a
  verdict on your intent. Diff two `ocr()` sets, watch one label, count rows,
  poll until something appears — whatever matches what you asked for.
- **Navigation:** `home()`, `back()`, `app_switcher()`, `open_app("Notes")`,
  `type_text("...")`, `press("enter")`, `long_press(x, y)`, `tap(x, y)`.
- **`scroll` says what you want to SEE; `swipe` says which way the finger
  goes.** English disagrees the same way — "scroll down the page" and "swipe
  up for the next video" describe the same motion. `scroll("down")` reveals
  what is further down; `swipe("up")` is a thumb flick, the phrasing everyone
  uses for "next". `scroll`, `scroll_screen`, `scroll_until` and
  `scroll_collect` take the content direction; only `swipe` takes finger
  motion. `"left"`/`"right"` work on both. Which one moves what differs per
  phone — see each section.
- **Walking a list:** `scroll_until(done)` stops when your predicate on the
  visible boxes is met; `scroll_collect(extract, key=...)` walks and de-dupes,
  returning `{items, stop, scrolls}` with `stop` of `'reached-end'` or
  `'max-scrolls'`. Both end on YOUR check, so an extractor that misses rows
  ends the walk early. `scroll_screen()` is the single step and returns
  `{before, after, boxes}` for you to judge. `at=` aims the gesture at an
  inner list or strip; only the scroll view under that point moves.
- **`type_text` needs a focused field** on every phone. Tap the field, wait for
  the keyboard, then type; verify with `ocr()`, because unfocused text goes
  elsewhere or nowhere.
- **Consent.** This is the user's real phone and real accounts. Stop and ask
  before anything outward-facing or hard to reverse: sending, posting,
  following, purchasing, deleting, changing settings. Never type a PIN, a
  password or a 2FA code; on a cloud phone, hand the controls over instead
  (`phone-harness cloud open`, below).
- **Connection is the user's job.** Pairing, unlocking, tapping Allow,
  locking an iPhone that says "iPhone in Use", approving a cloud sign-in in
  the browser: when the harness says the phone is not reachable, relay its
  message and ask — never tap through a Connect screen and never loop-poll.
  Retry once after the user says it is done.

## Cloud phones

`phone-harness cloud start` rents the user's own Android phone from Phone
Harness Cloud and connects to it; from then on every script drives that phone
with nothing exported, exactly as the Android section describes. It is the
same phone each time: apps, logins and normally the exact screen are kept
between sessions.

```bash
phone-harness cloud                     # signed in? a phone attached? minutes left?
phone-harness cloud start               # ~15s; opens the live view; safe to run twice (reattaches)
phone-harness cloud start --minutes 25  # default 15, cap 30 (config: cloud.minutes, cloud.max_minutes)
phone-harness cloud start --temp        # a throwaway phone that keeps nothing
phone-harness cloud ls                  # every running session (* = attached)
phone-harness cloud phone               # the saved phone: stored / running / saving
phone-harness cloud watch               # reopen the read-only live view (--print to share the link)
phone-harness cloud open                # the dashboard: the user takes the controls (their sign-in)
phone-harness cloud stop                # ends billing and saves the phone; returns at once
```

- **It bills by the minute while it is up.** Start it once and keep it for the
  whole conversation — a stop and a restart between two requests wastes more
  than it saves. Stop it when the user is done, and say that you did.
- **Stop before the deadline; a session cannot be extended.** One that simply
  runs out keeps the phone's data but not its running state. The harness warns
  on stderr under two minutes; `phone-harness cloud` shows the time left. If
  the task needs longer, stop and start again — the phone comes back where it
  was in about 15 seconds. `cloud stop` returns at once; the save finishes on
  its own, and a `cloud start` during it waits.
- **A session the user started is theirs.** `cloud start` attaches to a phone
  that is already running instead of starting another; leave it running unless
  they ask you to stop it.
- **`Not signed in`** means the user runs `phone-harness cloud login` and
  approves it in a browser. Relay that; you cannot do it for them. A user
  without an account is pointed at phone-harness.com by the CLI itself.
- **Watching versus controlling.** `cloud start` opens the phone's live view
  in the user's browser so they can watch you work; it is read-only and
  `cloud watch` reopens it. When the user wants to drive themselves — a
  password, a 2FA prompt, or just to take over — run `phone-harness cloud open`
  (the dashboard, behind their own sign-in) and wait for them to say they are
  done before you touch the phone again.
- The connection is handled for you, including reconnecting after a drop. The
  adb address the CLI shows is not a secret; the unlock code is, and you never
  need it — do not look for it, print it, or ask the user for it.

## Android

The phone is reached over adb — a USB phone if plugged in, else the paired
Wi-Fi phone, else the attached cloud phone — so there is nothing to select.
`phone-harness android` shows known phones and what is attached.

```bash
PHONE_HARNESS_PLATFORM=android phone-harness <<'PY'
open_app("chrome"); wait_for_app("com.android.chrome")
tap_ui("Got it")                    # exact label from the accessibility tree
PY
```

- **Coordinates are device pixels;** the screenshot is 1:1 with `tap(x, y)`.
- **`ocr()` is the accessibility tree** (`source: "tree"`) — exact, no
  misreads. Prefer `ui()` / `find_nodes()` / `tap_ui()`: they also see
  elements with no visible text (icons with a content-description, fields by
  resource-id like `tap_ui("url_bar")`).
- **Some screens never give up their tree** — a playing video, some Settings
  pages. On a Mac, `ocr()` then reads the screenshot with Vision instead
  (`source: "pixels"`, fuzzier, still tap-ready; the first read costs ~12s
  while uiautomator gives up, later reads are fast). Elsewhere it raises
  saying so; take a `screenshot()` and look at it instead of retrying.
  `ui()` / `tap_ui()` need the real tree and keep raising on such screens.
- **adb is the native language here, and it is first-class.** `shell(cmd)`
  runs `adb shell cmd` on whichever phone the harness chose: `shell("input tap
  360 640")`, `shell("input keyevent KEYCODE_BACK")`, `shell("am start -n
  pkg/.Activity")`, `shell("dumpsys notification --noredact")`, `shell("pm list
  packages -3")`. The input helpers are one-line wrappers over the same
  commands — use whichever you think in. What the harness adds: finding and
  reconnecting the phone, and the screen as a short list instead of XML.
- **`scroll` and `swipe` are different gestures here.** `scroll` moves the
  content and stops: no momentum, the same distance every time, so use it (and
  `scroll_until` / `scroll_collect`) to walk a list without skipping rows.
  `swipe` is a flick and coasts past whatever was next — right for "next
  video" or changing pages, wrong for reading a list.
- `open_app("TikTok")` takes the name a person would say, a package id, or a
  fragment of one, and returns the package it launched; when nothing matches,
  the error lists what is installed. `back()`, `current_app()`, `list_apps()`
  exist.
- `press()` takes single keys only (`"enter"`, `"back"`, `"tab"`); chords
  raise Unsupported. `type_text` types ASCII: adb cannot type emoji or
  accented letters.
- **Verify cheaply, then read.** adb reports nothing about outcomes — a tap on
  empty space "succeeds". After an action: `wait_for_app(...)` (~0.1s a poll)
  or `wait_for_text(...)` (returns the box or None), then `ui()`/`ocr()` once.
  The tree costs ~2-3s a call on a slow phone and a screenshot ~0.5s, so
  batching a proven sub-task is worth a lot; a batch of unverified steps fails
  silently and tells you nothing about which one broke.
- No focus to keep: nothing on the Mac has to be frontmost, and
  `interruption(before, after)` always reports nothing disturbed.
- **A desk phone locks itself** after its screen timeout. `connection_state()`
  reports `locked`; taps and `ocr()` refuse with the same message. Ask the user
  to unlock — never type a PIN. `screenshot()` still works locked, so you can
  show them what you see. For a task longer than a minute, ask the user, then
  `phone-harness android awake --bg` keeps it awake without changing any phone
  setting; `phone-harness android rest` ends that. Cloud phones do not lock.
- Connecting a desk phone is the user's job (USB debugging + Allow, or
  Wireless debugging + `phone-harness android pair CODE`); on `no-device` the
  error names the missing step — relay it, don't retry-loop.

## iPhone (iPhone Mirroring)

The Mac's iPhone Mirroring app renders the phone as a window; the harness
captures that window and OCRs it with Vision for eyes, and posts HID-level
events into it for hands. All coordinates are global macOS screen points.

- `ensure_mirroring()` launches the window and gates on connection. The
  default build works the phone **without taking the user's focus**: capture
  is by window id and taps and keystrokes are event records delivered straight
  to the app. Scrolling is the exception — macOS routes a scroll to whichever
  window sits under the pointer, so a scroll raises the mirroring window for
  the length of the gesture and hands focus straight back. Expect a brief
  flicker on scrolls and nothing on anything else. `PHONE_HARNESS_BACKGROUND=0`
  forces the classic path, which focuses before every action.
- **Icons without labels:** `screenshot()`, view the image, and use
  `tap_image_point(x, y, image_size=...)` with coordinates measured in the
  screenshot. Do **not** pass screenshot pixel coordinates to `tap()`: it
  expects global screen points. If using `tap()`, convert with `image_point()`
  from the current `screen_info()`; never estimate the window offset.
- **Use `scroll` for anything scrollable.** On macOS 26 a vertical touch-drag
  is dropped, so `swipe("up")`/`swipe("down")` move nothing in a list or a
  feed — measured on Settings and on TikTok. Horizontal still works, so
  `swipe("left")` / `swipe("right")` remain the way to flip Home Screen pages
  and carousels, which a scroll cannot do. (Breaking change: `scroll` used to
  take finger motion too, so the old `scroll("up")` is today's
  `scroll("down")`. `swipe` is unchanged.)
- `open_app("Notes")` goes through Spotlight.
- **Home-Screen labels are not tap targets.** `tap_text("Weather")` hits the
  label and nothing happens; the icon is ~35 points above it. Use
  `tap_icon("Weather")` (agent helper) on the Home Screen; `tap_text` works
  for in-app buttons and list rows.
- **`type_text` pastes; it does not type.** The keystroke path runs through
  iOS autocorrect, which rewrites words as they land ("Thu" becomes "thru").
  Pass `keystrokes=True` for fields that need real key events. The typed text
  stays on the Mac clipboard afterwards. If a tap will not take focus,
  `press("tab")` moves between fields.
- **Connecting is the user's job, and so is resuming.** `ensure_mirroring()`
  raises a clear message when the phone is not connected (`connection_state()`
  is `ready` / `blocked` / `no-window` / `not-running`). **STOP and relay it.**
  Never tap `Connect` / `Continue` and never loop-poll: tapping Connect while
  the phone is unlocked does nothing, and the only fix is the user locking or
  connecting the phone. **Unlocking the physical phone pauses the session**
  ("iPhone in Use") — do not tap through the resume screen.
- **Unfocused input is swallowed silently — for events you post yourself.**
  The helpers are immune in the background build, but raw CGEvents and the
  `PHONE_HARNESS_BACKGROUND=0` path need the window frontmost: `activate()`
  before posting, and re-activate if a click steals focus. The failure looks
  exactly like "scrolling is broken" or "the list already ended" — when a
  gesture changes nothing on screen, check focus before inventing another
  theory.
- **The window is a video stream.** macOS accessibility sees nothing inside
  it; AppleScript `click at` fails silently. Only HID-level CGEvents work.
- **The window moves.** Never cache coordinates across calls; `ocr()` and
  `swipe()` re-query bounds every time.
- Mouse taps map to touches 1:1, but there is no multi-touch: no pinch, no
  two-finger gestures.
- Raw Quartz is the escape hatch: `import Quartz` in your script for anything
  the helpers don't cover — but raw CGEvents don't ride the helpers' delivery
  path, and where they land is its own question per event type. Check what
  actually happened on screen rather than assuming the event arrived.

For task-specific edits, use `agent-workspace/agent_helpers.py`. For setup or
permission problems, read `install.md`.
