---
name: phone-harness
description: "Control the user's phone - an iPhone through the Mac's iPhone Mirroring window, an Android over adb, a rented cloud Android, or a cloud iPhone over HTTPS: open apps, tap, type, swipe, read the screen."
---

# phone-harness

The main helpers are listed in [Helpers](#helpers).

Direct control of a phone from Python scripts. The same helpers drive every
kind of phone below; what differs is how each one sees and touches the screen,
and that is what the per-phone sections below are for. Read **Which phone**, the
shared **Working method**, and then only the section for the phone in front
of you.

## Setup

Check what this machine can do (OS, shell, install rights, network,
attached phones; `phone-harness doctor` does the phone part), tell the user
in a sentence or two which phone you can drive with what is here, and use
the best one that actually runs. A phone on the desk wins; the cloud needs
no hardware and is always the fallback.

| Mode | Needs |
| --- | --- |
| Personal iPhone | macOS 15+, iPhone Mirroring paired to an iOS 18+ iPhone, Accessibility + Screen Recording for the app that launches the command (`phone-harness doctor ios` checks them) |
| Android on the desk | macOS, Linux or Windows; adb; USB debugging (or Wireless debugging, Android 11+) |
| Cloud iPhone | HTTPS to phone-harness.com hosts; a signed-in account. No adb, any OS |
| Cloud Android | the cloud iPhone list, plus adb and outbound TCP to the phone's adb port |
| No install possible | HTTPS and an HTTP client: the API directly |

- If something is missing, name it and the one step the user can take.
- Ask the user only to approve sign-in and to pick a phone.
- Detail: [iPhone](https://github.com/ShawnPana/phone-harness/blob/main/setup/ios.md), [Android](https://github.com/ShawnPana/phone-harness/blob/main/setup/android.md),
  [Cloud](https://github.com/ShawnPana/phone-harness/blob/main/setup/cloud.md), [API](https://github.com/ShawnPana/phone-harness/blob/main/setup/api.md).

## Helpers

- `ensure_device()` - Screen bounds once the device is usable, or raises with what the user has to do.
- `screen(min_confidence=0.3)` - Visible text as `{text, confidence, source, x, y, w, h}` boxes. On Android the boxes come from the accessibility tree; on iPhone Mirroring they come from recognizing text.
- `screenshot(path=None)` - Capture the screen to a PNG, print its path, and return it.
- `find_text(query, exact=False)` - Visible text boxes matching `query`.
- `tap_text(query, index=0, exact=False)` - Tap the center of visible text matching `query`.
- `tap(x, y)` - Tap one point.
- `long_press(x, y, duration=0.8)` - Press and hold a point.
- `type_text(text, delay=0.03, keystrokes=False)` - Type into the focused field.
- `press(combo)` - Press a key, such as `press("enter")`.
- `open_app(name)` - Launch an app and return the app id that launched.
- `open_url(url)` - Open an https link in the phone's browser, whole. Cloud phones only for now.
- `push_file(path)` - Send a file to the phone; it opens in the phone's browser under its own name. Cloud phones only for now.
- `swipe(direction, distance=0.4, at=None)` - Flick. `direction` is finger motion: `"up"`, `"down"`, `"left"`, or `"right"`.
- `scroll(direction="down", amount=0.3, at=None)` - Scroll. `direction` is the content to reveal: `"down"`, `"up"`, `"left"`, or `"right"`.
- `scroll_screen(direction="down", amount=0.6, settle=2.5, moved_thresh=None, at=None)` - One scroll, then the text on screen before and after.
- `scroll_until(done, direction="down", amount=0.6, max_scrolls=60, settle=2.5, at=None)` - Scroll until `done(boxes)` is truthy or the screen stops changing.
- `scroll_collect(extract=None, key=None, direction="down", amount=0.6, max_scrolls=400, end_after=3, settle=2.5, on_progress=None, at=None)` - Scroll a list and return `{items, stop, scrolls}`.
- `home()` - Go to the Home Screen.
- `back()` - System Back, where the phone has one.
- `app_switcher()` - Open the app switcher.
- `wait_stable(timeout=6.0, interval=0.5, settle=2)` - Wait until the screen stops changing.
- `wait_for_text(query, timeout=10.0, exact=False, interval=0.5)` - Poll until `query` is visible; return its box, or None.
- `wait_for_app(app_id, timeout=10.0, interval=0.3)` - Poll until `app_id` is the foreground app.
- `ui()` - The accessibility tree, where the device has one.
- `tap_ui(query, index=0, exact=False, clickable_only=False)` - Tap an element in the accessibility tree.
- `current_app()` - Foreground app id, where the device exposes one.
- `list_apps(include_system=False)` - Installed app ids.
- `screen_info()` - `{window, frontmost, img_px}`: bounds, focus, and capture size.
- `find_window()` - Screen bounds `{x, y, w, h, id}`, or None if there is no device.
- `image_point(x, y, image_size=None)` - Convert a screenshot pixel to a screen point.
- `tap_image_point(x, y, image_size=None)` - Tap a point taken from a screenshot.
- `connection_state()` - Backend state string; `"ready"` means usable.
- `shell(cmd, binary=False, timeout=60)` - Run a shell command on the phone, where the device has a shell.

## Which phone?

| Phone | How it is reached | Eyes | Hands | Read |
| --- | --- | --- | --- | --- |
| **Cloud Android** - rented from Phone Harness Cloud, the user's own saved phone | `phone-harness cloud start` connects it; nothing to export | accessibility tree (exact), Vision OCR fallback on a Mac | adb `input` | Working method, then **Cloud phones** and **Android** |
| **Cloud iPhone** - a second iPhone hosted by Phone Harness Cloud, not the one in the user's pocket; an iOS entry in the account catalog | `phone-harness cloud start` with its id, label, or `ios` when that is the only one | server-side OCR (`source: "pixels"`), from any OS | ops over HTTPS | Working method, then **Cloud iPhone** |
| **Android on the desk** - USB or paired Wi-Fi | the harness finds it | accessibility tree (exact), Vision OCR fallback on a Mac | adb `input` | Working method, then **Android** |
| **Personal iPhone** - the user's own phone, through the Mac's iPhone Mirroring window | the harness opens Mirroring and presses Connect; the user locks the phone | screenshots + Vision OCR | HID-level CGEvents into the window | Working method, then **iPhone** |

**"My phone" means the personal one.** The personal iPhone and the cloud
iPhone are two different devices with different apps and logins: the first is
in the user's pocket and mirrored onto the Mac, the second lives in a data
centre and is reached over HTTPS. When the user says "my phone", "my iPhone",
or names an app they use, drive the personal iPhone. Use a cloud phone only
when the user names it ("the cloud phone", `cloud start`) or the task is
clearly about it. If the personal phone cannot be reached, say so and ask;
never fall through to a cloud session that happens to be running, even one
the user started - it is theirs, and it is a different phone.

`phone-harness config` shows the default platform (`ios` on a Mac, `android`
elsewhere) and every other setting. `phone-harness cloud` shows whether a cloud
phone is attached; while one is, scripts drive it regardless of the config
default. `PHONE_HARNESS_PLATFORM=android` overrides per call. On a cloud
iPhone any explicit platform selects a local backend; see **Cloud iPhone**.

**When not to use any of them:** if the task is doable on the Mac or the web -
a website, an API, an app with a web equivalent - do it there and leave the
phone alone. Use a phone when the task genuinely needs one: phone-only apps,
things tied to a phone number or 2FA, checking how something looks on a phone.

## Working method (every phone)

```bash
phone-harness <<'PY'
# task: report the OS version and model name from Settings
# step: open Settings, find About, read the screen
open_app("Settings")
print([o["text"] for o in screen()][:10])
PY
```

- Invoke as `phone-harness` with a heredoc. Helpers are pre-imported. Start
  every script with `# task:` (the user's request in one sentence, identical
  across the scripts of one request) and `# step:` (what this script does).
- **Tell the user what you are doing as you go.** A phone task is many short
  scripts and the user sees none of them: one line before each script (what
  you are about to do), one line after (what you saw). Never run two scripts
  in a row in silence. The `# task:`/`# step:` comments are not this - the user
  cannot see them.
- **Read with `screen()`, not by eyeballing screenshots.** Every visible string
  comes back with a tap-ready centre: `[{text, confidence, source, x, y, w, h}]`.
  Filter in Python before printing. `tap_text("Weather")` taps by label and,
  on failure, raises with what IS visible - read the exception before retrying.
  `screenshot()` costs more but shows what OCR cannot: icons, images, state.
- **Act, verify, adapt.** There is no DOM to assert against and no return value
  that means "it worked", so:
  1. Name what should change before you act - a title, a row, a field's
     contents. Most phone failures are silent no-ops; if you cannot name the
     expected change you cannot tell success from one.
  2. Do one thing, then check that one thing: `wait_for_text("Got it")`,
     `wait_for_app("com.android.chrome")` or `wait_stable()`, then one `screen()`.
     Do not `wait(n)` for a screen to load.
  3. Once a sequence is proven, batch it: a whole sub-task in one script is far
     faster than a call per turn. Keep one cheap check at the end.
  4. When a check fails, isolate: re-run that one action, look at the screen,
     form one guess, test it. Do not re-run the batch hoping it lands.
  5. Keep what you learn in the agent workspace's `agent_helpers.py`.
     A pip or uv install uses `~/.phone-harness/agent-workspace`, or
     `~/.local/share/phone-harness/agent-workspace` when
     `~/.phone-harness` is a git checkout, so an upgrade does not wipe
     it. A checkout of this repo uses its own `agent-workspace/`.
     `PH_AGENT_WORKSPACE` overrides that path. The directory is
     created from the defaults only when it is missing. An existing
     workspace is never overwritten. To get a fresh default, delete the
     workspace folder; it is recreated on the next run.
- **The harness reports, you decide.** Helpers return observations, never a
  verdict on your intent. Diff two `screen()` sets, watch one label, count rows,
  poll until something appears - whatever matches what you asked for.
- **Navigation:** `home()`, `back()`, `app_switcher()`, `open_app("Notes")`,
  `type_text("...")`, `press("enter")`, `long_press(x, y)`, `tap(x, y)`.
- **`scroll` says what you want to SEE; `swipe` says which way the finger
  goes.** English disagrees the same way - "scroll down the page" and "swipe
  up for the next video" describe the same motion. `scroll("down")` reveals
  what is further down; `swipe("up")` is a thumb flick, the phrasing everyone
  uses for "next". `scroll`, `scroll_screen`, `scroll_until` and
  `scroll_collect` take the content direction; only `swipe` takes finger
  motion. `"left"`/`"right"` work on both. Which one moves what differs per
  phone - see each section.
- **Walking a list:** `scroll_until(done)` stops when your predicate on the
  visible boxes is met; `scroll_collect(extract, key=...)` walks and de-dupes,
  returning `{items, stop, scrolls}` with `stop` of `'reached-end'` or
  `'max-scrolls'`. Both end on YOUR check, so an extractor that misses rows
  ends the walk early. `scroll_screen()` is the single step and returns
  `{before, after, boxes}` for you to judge. `at=` aims the gesture at an
  inner list or strip; only the scroll view under that point moves.
- **`type_text` needs a focused field** on every phone. Tap the field, wait for
  the keyboard, then type; verify with `screen()`, because unfocused text goes
  elsewhere or nowhere.
- **Consent.** This is the user's real phone and real accounts. Stop and ask
  before anything outward-facing or hard to reverse: sending, posting,
  following, purchasing, deleting, changing settings. Never type a PIN, a
  password or a 2FA code; on a cloud phone, hand the controls over instead
  (`phone-harness cloud open`, below).
- **The harness reconnects what it can; the rest is the user's.** On a
  personal iPhone, start the task with `ensure_device()`: it opens iPhone
  Mirroring if it is closed, presses the window's Connect once, and brings
  the window to the front so the user can watch. Pairing, unlocking, tapping Allow,
  locking an iPhone that says "iPhone in Use" or "Timed Out", approving a
  cloud sign-in in the browser: when the harness still says the phone is not
  reachable, relay its message and ask - never loop-poll. Retry once after
  the user says it is done; that retry presses Connect again.

## Cloud phones

There are two things. A **phone** is a row in the account catalog (`GET /me`
-> `available`): a label, a platform, an id, and whether it is a temporary
phone. The service keeps no default phone; a preference is the chooser's. A **session** is one of those phones rented right now.
`cloud ls` prints one row per catalog phone and writes any live session on
that row (`*` is the session this process targets). A session lines up with
the row that has the same device_id or profile_id; a session with neither
lines up with the temporary row. A cloud iPhone session does not echo that
id: its `device` is the word "iPhone", and its `profile` (`iphone-...`) matches
no catalog row. The catalog row's `state` is `running` while a session holds
it. A session whose ids match no row counts as unmatched. When exactly one
row of that platform and kind is `running` and exactly one unmatched session
has that same platform and kind, the session is written on that row. A
session that still matches nothing is listed on its own.

`cloud start` with no name starts this machine's `cloud.phone` setting
(`config set cloud.phone "Desk phone"`) when one is set; otherwise it sends
no `platform` or `kind` and gets the service's rule for an unnamed start
(`GET /me` -> `unnamed`): a temporary Android. A name is a catalog id
or label, or `ios` / `android` when only one entry has that platform. Several
matches print the catalog and stop (a terminal may ask which one). The CLI
turns the chosen row into `platform` and `kind`, plus `device_id` for an
iPhone or `profile_id` for the saved Android. The temporary row sends neither
id.

What the helpers do next follows the session's link, not the platform name.
An adb link is the Android section. A control link (`{url, token}`) is
**Cloud iPhone**.

```bash
phone-harness cloud                          # signed in? which session is attached? time left?
phone-harness cloud ls                       # catalog, live sessions inlined; --json for the rows
phone-harness cloud start                    # cloud.phone if set, else a temporary Android; opens the live view
phone-harness cloud start ios --for 15m      # one iPhone, fifteen minutes
phone-harness cloud start "Temporary phone" --for 90s
phone-harness --session SID <<'PY'           # this process only; or PHONE_HARNESS_SESSION
print(screen_info())
PY
phone-harness cloud stop                     # ends the session; returns at once
phone-harness cloud phone                    # the saved Android's stored state
phone-harness cloud watch                    # reopen the read-only live view (--print to share)
phone-harness cloud open                     # the dashboard: the user takes the controls
```

- **Pin a session when more than one can be up.** `--session SID` or
  `PHONE_HARNESS_SESSION` selects one phone for this process, and every
  `cloud` command and helper honours it. With neither, a person at a terminal
  gets the attached session: the last `cloud start`, or `cloud use SID`.
  `cloud use` rewrites that shared fallback. Parallel agents should pass
  `--session` and leave `cloud use` alone, or they will steal the fallback
  from each other.
- **A cloud Android bills by the minute while it is up; a cloud iPhone does
  not.** Start it once and keep it for the whole conversation. Stop an Android
  when the user is done, and say that you did. An iPhone can stay running.
  `--for` is the length: `90s`, `15m` / `15min`, or a bare number of minutes.
  Default 15, cap 30 (`cloud.minutes`, `cloud.max_minutes`).
- **Stop before the deadline; a session cannot be extended.** One that simply
  runs out keeps a saved Android's data but not its running state. The harness
  warns on stderr under two minutes; `phone-harness cloud` shows the time left.
  `cloud stop` returns at once. Stopping an adb phone that has a profile saves
  it; `cloud start` of that phone waits out a save still in progress. A
  control link keeps its data on the phone, so there is nothing to wait for.
- **Already running.** Starting a phone this machine already holds for the
  same catalog choice reuses that session. If the service says it is held
  elsewhere (`409` `profile_running`), the message names the SID and says to
  attach with `--session SID`. Do that; do not start a second one. `409`
  `device_unavailable` means the phone is granted but not on its host.
  `409` `session_limit` names how many sessions are in use. `503`
  `phones_busy` includes `Retry-After`. `403` `account_blocked` means rentals
  are blocked; a `403` about credit or balance means add credit. A `400` that
  names no such phone reprints the catalog.
- **`Not signed in`** means the user runs `phone-harness cloud login` and
  approves it in a browser. Relay that; you cannot do it for them.
- **Watching versus controlling.** `cloud start` opens the live view so the
  user can watch. `cloud watch` reopens it. For a password, a 2FA prompt, or
  the user taking over, run `phone-harness cloud open` and wait until they say
  they are done.
- **`cloud phone` is the saved Android only** (`GET /me/profile`).
  `cloud phone reset --yes` wipes that phone's stored state: apps, logins,
  and data. The CLI has no iPhone reset; that is the operator's.
- **A cloud session that ends does not hand you another phone.** If the
  session this command is using expires, the error says `session <sid> expired`.
  If the service loses it, the error says the cloud phone failed and is
  unreachable. That command stops. It does not drive another phone, and the
  CLI does not rent a replacement. The saved note of that dead session is
  removed once the error has been reported, or once `phone-harness cloud` has
  shown it once, so later status output does not repeat it. A command that
  hits the dead session while that note is still there fails with the same
  message. Start the same catalog phone again with a longer duration and
  continue the task: `phone-harness cloud start <same phone> --for <longer>`.
  `<same phone>` is that phone's id or label (or `ios` / `android` when only
  one entry has that platform). `<longer>` is a duration longer than the
  session that just ended (`90s`, `15m`, or a number of minutes). A
  `--session` pin whose session expired is the same error, naming that sid.
  A missing cloud state file still means no cloud phone was attached, and the
  personal phone is used as today. An unreadable state file is an error, not
  a cue to switch phones. `cloud stop` and a new `cloud start` also drop a
  note that has not been reported yet.
- The adb address the CLI shows is not a secret; the unlock code is. Do not
  look for it, print it, or ask the user for it.

## Cloud iPhone

Start it from the catalog: `phone-harness cloud start ios` when that is
unambiguous, or the entry's id or label (`cloud ls`). The create body is
`platform: "ios"`, `kind: "device"`, and `device_id` set to that entry's id.
A ready session carries `control: {url, token, expires_at}` and no `adb`.
From then on the helpers drive it over HTTPS. This machine needs no adb, no
shell, and no Mac. It is a phone granted to the user's account and kept
between sessions - not the personal iPhone mirrored on the Mac, which is a
different device with its own apps and logins. It does not bill by the
minute, so there is no need to stop it to save credit, or to ask before
starting it. `cloud stop` returns at once; the phone keeps its own data. There is no Android-style
disk save, and nothing on this phone for `cloud phone` to wipe.

An Android session can stay up beside it. Target one with `--session SID`.
`cloud use SID` only changes the terminal fallback.

Length, `ls`, `watch`, and `stop` are the **Cloud phones** commands
above. An iPhone session reports `billing_mode: disabled`: no per-minute charge. Run scripts as
plain `phone-harness` (with `--session` when you must pin one).
`PHONE_HARNESS_PLATFORM=...` selects a local backend and skips the attached
cloud phone.

The mirroring section describes a Mac window. This phone is the HTTPS path.

The client posts `{"op", "kw"}` to `<control url>/op` and reads the host's
list from `GET <control url>/ops` (`remote.py`). `supports(op)` is that list.
`send` of an op the host did not list raises `Unsupported` before the POST
(`this cloud phone cannot '...'`). The op names and the shapes below are the
client vocabulary in `transport.py`. Result shapes are what this repo
documents or decodes.

| Op | Arguments | Result this client uses |
| --- | --- | --- |
| `screen.capture` | `path` optional | `(png_path, bounds)`. On the wire the host returns `png_b64` and `bounds`; the client writes the PNG. Timeout 60s. |
| `screen.bounds` | | `{x, y, w, h, id}` or None |
| `screen.require` | | bounds, or raise |
| `screen.text` | `min_confidence` | `[{text, confidence, source, x, y, w, h}]`. `source` is `"pixels"` or `"tree"`. |
| `screen.text_pixels` | `min_confidence` | same, forced through pixel OCR |
| `input.tap` | `x, y` | host result, passed through |
| `input.press` | `x, y, duration` | host result. Long press. |
| `input.drag` | `x1, y1, x2, y2, duration, steps` | host result |
| `input.scroll` | `x, y, dy, dx, steps` | host result. Content-space deltas. |
| `input.keys` | `combo` | host result |
| `input.text` | `s, delay, keystrokes` | host result. Timeout `max(60, 0.2 * len(s) + 30)` seconds. |
| `nav.home` / `nav.back` / `nav.recents` | | host result, or `Unsupported` when the host says `unsupported` |
| `apps.launch` | `name`, `fresh` | app id. Timeout 90s. |
| `apps.current` | | app id or None |
| `apps.list` | `include_system` | `[app id]`. Timeout 90s. |
| `session.state` | | backend-defined string; `'ready'` when usable |
| `session.require` | | bounds, or raise telling the user what to do |
| `session.refocus` | | `None` here. The client does not call the host. |
| `session.detail` | | a string. Local. |
| `focus.probe` | | an opaque snapshot. Local: `(True,)`. |
| `focus.diff` | `before, after` | `{raised, stole_focus}`. Local: both false. |
| `url.open` | `url` | app id. Opens an https link in the phone's browser, whole. Timeout 60s. |
| `tree` | | `[node]` where the device has one |
| `raw` | backend-specific | host result |

Trust `GET /ops` when the host's list is shorter than this table.

### Files on a cloud iPhone

There is no shell, so bytes move through storage links the service signs and
the phone's `url.open` op. Two helpers wrap it, and they follow the
attached session (or `--session`) like every other helper. Files go to the
phone only; nothing comes back this way.

- `push_file(path)` uploads the file and opens it in the phone's browser
  under its own name (a JPEG shows as an image, an MP4 plays). From there
  the phone's own UI decides what to do with it; look at the screen and act
  on what is there, as with any other task.
- `open_url(url)` is the op behind both: any https link, opened whole, with
  no typing and no clipboard. Check `supports('url.open')` first; an older
  host lists no such op.

On a local phone both raise `Unsupported` ("cloud phones only for
now"). Links expire within fifteen minutes and with the session; the files
are removed when the session ends. A link is a credential: do not paste one
into notes or output.

## Android

The phone is reached over adb - a USB phone if plugged in, else the paired
Wi-Fi phone, else the attached cloud Android - so there is nothing to select.
`phone-harness android` shows known phones and what is attached.

```bash
PHONE_HARNESS_PLATFORM=android phone-harness <<'PY'
open_app("chrome"); wait_for_app("com.android.chrome")
tap_ui("Got it")                    # exact label from the accessibility tree
PY
```

- **Coordinates are device pixels;** the screenshot is 1:1 with `tap(x, y)`.
- **`screen()` is the accessibility tree** (`source: "tree"`) - exact, no
  misreads. Prefer `ui()` / `find_nodes()` / `tap_ui()`: they also see
  elements with no visible text (icons with a content-description, fields by
  resource-id like `tap_ui("url_bar")`).
- **Some screens never give up their tree** - a playing video, some Settings
  pages. On a Mac, `screen()` then reads the screenshot with Vision instead
  (`source: "pixels"`, fuzzier, still tap-ready; the first read costs ~12s
  while uiautomator gives up, later reads are fast). Elsewhere it raises
  saying so; take a `screenshot()` and look at it instead of retrying.
  `ui()` / `tap_ui()` need the real tree and keep raising on such screens.
- **adb is the native language here, and it is first-class.** `shell(cmd)`
  runs `adb shell cmd` on whichever phone the harness chose: `shell("input tap
  360 640")`, `shell("input keyevent KEYCODE_BACK")`, `shell("am start -n
  pkg/.Activity")`, `shell("dumpsys notification --noredact")`, `shell("pm list
  packages -3")`. The input helpers are one-line wrappers over the same
  commands - use whichever you think in. What the harness adds: finding and
  reconnecting the phone, and the screen as a short list instead of XML.
- **`scroll` and `swipe` are different gestures here.** `scroll` moves the
  content and stops: no momentum, the same distance every time, so use it (and
  `scroll_until` / `scroll_collect`) to walk a list without skipping rows.
  `swipe` is a flick and coasts past whatever was next - right for "next
  video" or changing pages, wrong for reading a list.
- `open_app("TikTok")` takes the name a person would say, a package id, or a
  fragment of one, and returns the package it launched; when nothing matches,
  the error lists what is installed. `back()`, `current_app()`, `list_apps()`
  exist.
- `press()` takes single keys only (`"enter"`, `"back"`, `"tab"`); chords
  raise Unsupported. `type_text` types ASCII: adb cannot type emoji or
  accented letters.
- **Verify cheaply, then read.** adb reports nothing about outcomes - a tap on
  empty space "succeeds". After an action: `wait_for_app(...)` (~0.1s a poll)
  or `wait_for_text(...)` (returns the box or None), then `ui()`/`screen()` once.
  The tree costs ~2-3s a call on a slow phone and a screenshot ~0.5s, so
  batching a proven sub-task is worth a lot; a batch of unverified steps fails
  silently and tells you nothing about which one broke.
- No focus to keep: nothing on the Mac has to be frontmost, and
  `interruption(before, after)` always reports nothing disturbed.
- **A desk phone locks itself** after its screen timeout. `connection_state()`
  reports `locked`; taps and `screen()` refuse with the same message. Ask the user
  to unlock - never type a PIN. `screenshot()` still works locked, so you can
  show them what you see. For a task longer than a minute, ask the user, then
  `phone-harness android awake --bg` keeps it awake without changing any phone
  setting; `phone-harness android rest` ends that. Cloud phones do not lock.
- Connecting a desk phone is the user's job (USB debugging + Allow, or
  Wireless debugging + `phone-harness android pair CODE`); on `no-device` the
  error names the missing step - relay it, don't retry-loop.
- **A saved primary that is unreachable is a failure, not a different phone.**
  If `phone-harness android use` has named a primary and that phone cannot
  be reached, the error says the primary is unreachable. Do not expect the
  harness to connect some other paired or mDNS phone instead. When no
  primary is configured, a USB phone that is plugged in is still used.

## iPhone (iPhone Mirroring)

The Mac's iPhone Mirroring app renders the phone as a window; the harness
captures that window and OCRs it with Vision for eyes, and posts HID-level
events into it for hands. All coordinates are global macOS screen points.
iPhone Mirroring stays the default on a Mac. Nothing checks permissions
unless you run `phone-harness doctor ios`.

- **macOS permissions, before the first tap.** Accessibility (taps and typing) and Screen Recording (seeing the window) belong to the app that launches the command, such as Terminal, iTerm, Cursor, Hermes, or the agent's own app, not to the shell. Under tmux or screen, that is the app that started the server, even when someone attaches from another app. `phone-harness doctor ios` reads both without prompting or opening anything and names the app to grant; `--json` is the same check for a script. When one is missing, ask the user to run `phone-harness doctor ios --fix` in a terminal on the Mac: a panel opens beside System Settings, they drag the app into the list and confirm with Touch ID or their password, and it moves on by itself as each grant lands (Not Now closes it; it gives up after 10 minutes). From a pipe, which is how most agent shells run, `--fix` opens the first missing pane and returns instead of waiting; without a display it prints the steps and opens nothing; `--yes` and `--non-interactive` never open System Settings and never wait. A plain SSH login is checked against the SSH service, so no grant made at the Mac reaches it and `--fix` only prints the steps: ask the user to start tmux in a terminal app at the Mac and run `phone-harness doctor ios --fix` there, then attach to that tmux session over SSH and run phone-harness inside it. If System Settings also lists the Python binary on its own row, that row needs turning on too. If the switch is already on and the state is `stale`, the app or the Python binary was updated or re-signed: remove that entry and add it again. If macOS offers to quit the app after the Screen Recording grant, Later is fine: a new phone-harness process already sees the grant, so do not restart the terminal.
- **Start every task with `ensure_device()`.** It is the first call on every
  phone, including iPhone Mirroring. It opens iPhone Mirroring
  if it is not running, brings the window to the front so the user sees what
  you see, presses the interstitial's Connect once through accessibility,
  and waits for the live stream. A phone that is in use shows up within
  about three seconds and it raises then, with the window's own words,
  rather than sitting out a timeout. It is the cheapest first line a script
  can have: a phone that cannot be driven fails there, not five taps in.
  Call it once in the first script of a task, not before every action.
  After that, the default build works the phone **without taking the user's focus**: capture
  is by window id and taps and keystrokes are event records delivered straight
  to the app. Scrolling is the exception - macOS routes a scroll to whichever
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
  feed - measured on Settings and on TikTok. Horizontal still works, so
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
- **What the harness cannot clear is physical.** Connect only works while
  the iPhone is locked; "iPhone in Use" and "Timed Out ... due to iPhone use"
  mean it was not. `ensure_device()` presses Connect once and then raises
  quoting the screen (`connection_state()` is `ready` / `blocked` /
  `no-window` / `not-running`). `connection_detail()` returns the labels and
  button titles in the window, with no screenshot, so you can see why it is
  blocked. **STOP and relay the error.** It names the next step: lock the
  iPhone when the window says in use, timed out, or lock your iPhone; approve
  or cancel when the Mac asks for Touch ID or a password (never type one);
  otherwise clear the window on the Mac. Retry once when the user says it is
  done. When the phone was in use, the app reconnects by itself the moment
  the phone is locked, so that retry usually finds it live; if not, it
  presses Connect again. Do not press it yourself in a loop and do
  not tap the window (an interstitial is a Mac view; taps meant for the
  phone go nowhere). **Unlocking the physical phone pauses the session**
  ("iPhone in Use") - the same rule applies mid-task. Taps, scrolls, swipes,
  typing, key presses, `open_app`, and `home` raise that same
  message and do not press Connect. `screenshot()` and `screen_info()` still
  work while the mirroring window is open, including when the state is
  blocked. Call `screenshot()` to see what the window shows. Ask the user
  to do what the error says, then call `ensure_device()` and continue from
  the step that raised.
  The Mac login prompt that
  sometimes appears in the window is never typed into.
- **Unfocused input is swallowed silently - for events you post yourself.**
  The helpers are immune in the background build, but raw CGEvents and the
  `PHONE_HARNESS_BACKGROUND=0` path need the window frontmost: `activate()`
  before posting, and re-activate if a click steals focus. The failure looks
  exactly like "scrolling is broken" or "the list already ended" - when a
  gesture changes nothing on screen, check focus before inventing another
  theory.
- **The window is a video stream.** macOS accessibility sees nothing inside
  it; AppleScript `click at` fails silently. Only HID-level CGEvents work.
- **The window moves.** Never cache coordinates across calls; `screen()` and
  `swipe()` re-query bounds every time.
- Mouse taps map to touches 1:1, but there is no multi-touch: no pinch, no
  two-finger gestures.
- Raw Quartz is the escape hatch: `import Quartz` in your script for anything
  the helpers don't cover - but raw CGEvents don't ride the helpers' delivery
  path, and where they land is its own question per event type. Check what
  actually happened on screen rather than assuming the event arrived.

For task-specific edits, use the agent workspace's `agent_helpers.py`:
`~/.phone-harness/agent-workspace` for a pip or uv install (or
`~/.local/share/phone-harness/agent-workspace` when `~/.phone-harness`
is a git checkout). A checkout of this repo uses its own
`agent-workspace/`. `PH_AGENT_WORKSPACE` overrides that. Installs seed
that directory from the packaged defaults when it does not exist yet;
an existing workspace is left as it is. To get a fresh default, delete
the workspace folder; it is recreated on the next run. For setup or
permission problems, run `phone-harness doctor ios` (add `--json` when you
need a machine-readable report) and read
https://github.com/ShawnPana/phone-harness/blob/main/install.md.

## Updates

`phone-harness --version` prints `phone-harness <version>`.

`phone-harness update` brings this install up to date. A uv tool runs
`uv tool upgrade phone-harness`. A git checkout runs `git pull --ff-only`
in that checkout. Anything else prints `pip install -U phone-harness` and
does not run it. The CLI never updates itself. Once a day, on a terminal,
it may print one stderr line when PyPI has a newer version:

```
phone-harness X.Y.Z is available (you have A.B.C): run phone-harness update
```

Relay that line to the user. `PHONE_HARNESS_NO_UPDATE_CHECK=1` turns the
notice off. A network problem never fails the command.
