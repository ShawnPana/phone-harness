---
name: phone-harness
description: "Control the user's iPhone through the Mac's iPhone Mirroring window: open apps, tap, type, swipe, read the screen — background by default (no mouse/focus steal)."
---

# phone-harness

Direct iPhone control via the iPhone Mirroring app — screenshots + Vision OCR
for eyes; **SkyLight background input** for hands by default (does **not** steal
the user's mouse or frontmost app). For task-specific helpers, use
`agent-workspace/agent_helpers.py` (loaded into every `phone-harness` session).
For setup or permission problems, read `install.md`.

Upstream: https://github.com/ShawnPana/phone-harness  
Background backend shipped 2026-08-10 (`background-input` + `background-by-default`).

## When Not to Use

If the task is doable on the Mac or the web — a website, an API, an app with a
web equivalent — do it there and leave the phone alone. Use phone-harness only
when the task genuinely needs the phone: iOS-only apps, things tied to the
user's phone number or 2FA, testing how something looks on the phone.

## Hard rules (do not re-learn these by breaking the phone)

### 1. Never delete, never jiggle-edit the Home Screen

- **Forbidden:** deleting apps, tapping red ⊖ / minus badges, Confirm Delete,
  rearranging icons "to clean up", entering edit mode on purpose.
- Home Screen **jiggle mode** (icons wiggle, ⊖ badges, top bar 编辑/完成) is
  almost always a mistake — caused by `long_press` on an icon, a sticky drag,
  or a hold that was meant to be a tap.
- On the Home Screen: **short `tap` only**. Do **not** call `long_press` on
  icons/folders unless the user explicitly asked for a long-press action.
- If jiggle/edit mode appears: **stop the task path**, call
  `exit_home_edit_mode()` (agent helper), verify badges are gone via
  `screenshot()`, then continue. Prefer tapping **完成** / Done; `home()` is
  a backup. **Never** tap a red minus while exiting.
- "Connection thrashing" (many swipes/long waits on the springboard) raises
  the odds of accidental edit mode — keep Home Screen navigation minimal.

### 2. `type_text` is ASCII / US-layout only — not Chinese

- iPhone Mirroring forwards **HID keycodes**, not Unicode. `type_text("大众点评")`
  raises `ValueError: cannot type '大' via keycodes`. Same for emoji and most
  CJK / full-width punctuation.
- For Spotlight / search fields when the target is Chinese:
  - Prefer **Latin brand / pinyin ASCII** that Spotlight already indexes
    (e.g. `dianping`, `meituan`, `wechat`) via `open_app("dianping")`.
  - Or use `paste_text("…")` (agent helper: Mac clipboard + `cmd+v`) after
    focusing the iOS field — requires Continuity clipboard / paste to work
    on that device; verify with `ocr()`/`screenshot()` after paste.
- Never invent a multi-step Chinese IME dance (switch keyboard, pinyin
  candidate picks) unless the user asks and you have verified it works.

### 3. Chinese UI: OCR is weak — you must read the screenshot image

- Vision OCR often **garbles or drops Chinese** (Home Screen labels, Dianping,
  WeChat, maps). English labels (YouTube, Gemini) survive; Chinese often becomes
  noise like `BJL9` / `SE IOL`.
- Workflow for any Chinese-heavy screen:
  1. `path = screenshot()` (copy under the workspace if needed)
  2. **Open/view the image** (do not rely on OCR text alone)
  3. Tap by computed geometry from `screen_info()` (img px → global points)
- Use `ocr()` as a helper for English strings and rough layout, not as ground
  truth for Chinese app names or ticket titles.

### 4. `connection_state() == ready` is not enough

Before driving the phone, prove the stream shows **real phone content**:

| Bad stream | Meaning | Action |
|------------|---------|--------|
| Window title like 欢迎使用 iPhone 镜像 / Welcome | Not paired / still on welcome | STOP — user must connect |
| Screenshot pure black / empty OCR | Session paused, wrong window, or not streaming | STOP — user must reconnect / lock phone |
| "iPhone in Use" / resume wall | Phone unlocked and stole the session | STOP — user locks phone |

- Do **not** tap Connect / Continue yourself (see Connection section).
- After the user says "connected", re-check with `screenshot()` + image view,
  not only `connection_state()`.

### 5. Opening apps

| Method | When | Notes |
|--------|------|--------|
| `open_app("Notes")` | App has a **Latin** Spotlight name | `cmd+3` → type → return; keyboard briefly steals Mac focus |
| `tap_icon("Weather")` | Home Screen, **OCR can read** the English/latin label | Taps ~35pt **above** the label |
| `screenshot` + tap geometry | Chinese app name / OCR garbage / icon in a folder | Required for 大众点评-style apps if Spotlight miss |
| Swipe Home pages | Last resort | Short `swipe("left")` only; no long-press; verify each page with **image**, not OCR alone |

If Spotlight has no app hit (only Mail/Notes/App Store suggestions), the app is
missing, renamed, or not indexed — **ask the user**, do not thrash every page
into jiggle mode.

### 6. Purchases and irreversible actions

Stop on the **payment confirmation** screen. Do not complete pay, send messages,
post, delete data, or change settings unless the user explicitly told you to
finish that step after seeing the confirmation UI.

## Background by default (do not steal focus)

**Default transport is `phone_harness.background`.** Mouse taps, long-presses,
drags, and scrolls are injected via private SkyLight `SLPSPostEventRecordTo`
into the iPhone Mirroring process. Capture uses `CGWindowListCreateImage` by
window id. Neither requires the mirroring window to be frontmost.

| Action | Steals focus / mouse? |
|--------|------------------------|
| `screenshot` / `ocr` / `screen_info` | No |
| `tap` / `long_press` / `drag` / `swipe` / `scroll*` | No |
| `type_text` / `press` (keyboard) | **Briefly yes** — keyboard event-record not implemented yet; falls back to classic path |

### Agent rules (mandatory)

1. **Use only `phone-harness` helpers** (`tap`, `ocr`, …). Do **not** hand-roll
   `CGEventPost`, `osascript … activate`, or AppleScript clicks for phone work.
2. **Do not** call `osascript` / `NSWorkspace.activate` / `open -a "iPhone镜像"`
   just to "make capture work". Background capture works unfocused when the
   stream is healthy.
3. **Do not** set `PHONE_HARNESS_BACKGROUND=0` unless the user asks, background
   import failed, or background capture repeatedly returns nothing **while**
   the user-confirmed stream is live (classic fallback steals focus — warn).
4. Treat this like Kimi Cu cursor-safe clicks: the user's frontmost Mac app stays
   theirs while you drive the phone.

### Env switch

```bash
# default — background, no focus steal
phone-harness <<'PY'
print(screen_info())
tap(x, y)
PY

# emergency only — classic CGEvent + frontmost (WILL steal mouse/focus)
PHONE_HARNESS_BACKGROUND=0 phone-harness <<'PY'
…
PY
```

If the background backend fails to import (SkyLight symbols differ by macOS
build), helpers auto-fall back to classic `mirror.py` and *will* focus the
window. Check with:

```bash
phone-harness <<'PY'
from phone_harness import helpers
print("background=", helpers._BACKGROUND, "module=", helpers.mirror.__name__)
PY
```

## Usage

```bash
phone-harness <<'PY'
print(screen_info())
print(connection_state())  # ready | blocked | no-window | not-running
# then always: screenshot + view image before trusting the session
PY
```

- Invoke as `phone-harness`. Use heredocs for multi-line commands.
- Helpers are pre-imported. All coordinates are **global screen points**.
- Under the background backend, `activate()` is a **no-op**. Prefer
  `connection_state()` / `ensure_mirroring()` over manually focusing the app.
- Agent helpers (`tap_icon`, `exit_home_edit_mode`, `paste_text`, …) load from
  `~/.phone-harness/agent-workspace/agent_helpers.py`.

## Screen Workflow

- Prefer `ocr()` for **English** chrome and rough layout; for Chinese, **view
  the screenshot image** (see Hard rule 3).
- Tap by label: `tap_text("Weather")`. On failure it raises with what IS
  visible, so read the exception before retrying.
- Icons without reliable labels: `screenshot()`, view the image, compute the
  point (image px ÷ scale + window origin — `screen_info()` has both sizes),
  then `tap(x, y)`.
- **Verify after every action**: short `wait` / `wait_stable()`, then
  `screenshot()` (and `ocr()` when useful). Capture is the ground truth.
- Navigation: `home()`, `app_switcher()`, `open_app("Notes")` (Spotlight,
  Latin names only), `swipe("up")`, `scroll()`, `type_text("ascii...")`,
  `paste_text("中文…")`, `press("return")`. Avoid `long_press` on Home Screen.
- **Scrolling a list**: use `scroll_collect(extract, key=...)` to walk a list
  to its true end, de-duping as it goes — it returns `{items, stop, scrolls}`
  where `stop` is `'reached-end'` or `'max-scrolls'`. Use `scroll_until(done)`
  to stop when a predicate on the visible OCR is met. Both decide "done" from
  whether the **screen actually moved**, not from whether your parser found
  new rows. Background scroll uses **momentum flicks** (wheel events do not
  route when unfocused).
- Raw Quartz is the escape hatch only for non-input needs. **Never** replace
  `tap`/`drag` with your own `CGEventPost` — that reintroduces focus steal.

## Consent

This is the user's real phone. Stop and ask before anything outward-facing or
hard to reverse: sending a message, posting, purchasing, **deleting anything
(including apps)**, changing settings. Navigating and reading for the user's
own task is fine, but don't linger in personal content (Messages, Photos, Mail)
beyond what the task needs.

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
- Also STOP on black frames / welcome-title windows even if state says `ready`
  (Hard rule 4).

## Gotchas

- **Do not steal the user's Mac focus.** Stay on the background backend. No
  `osascript activate`, no raw HID mouse injection outside helpers.
- **Keyboard still steals briefly** (`type_text` / `press` / `paste_text`).
  Warn if you will type a lot, or batch typing.
- **The window is a video stream.** macOS accessibility sees nothing inside
  it; AppleScript `click at` fails silently.
- **The window moves.** Never cache coordinates across calls; `ocr()` and
  `swipe()` re-query bounds every time.
- **Unlocking the physical phone pauses the session** ("iPhone in Use"). Do not
  tap through the resume screen — stop and ask the user to lock/connect the
  phone (see "Connection is the user's job").
- **`type_text` needs an iOS text field focused first** — tap the field, wait
  for the keyboard, then type or paste. ASCII only for `type_text`.
- **Home-Screen labels are not tap targets.** `tap_text("Weather")` hits the
  label and nothing happens; the icon is ~35 points above it. Use
  `tap_icon("Weather")` (agent helper) on the Home Screen; `tap_text` works
  fine for in-app buttons and list rows.
- Mouse taps map to touches 1:1, but there is no multi-touch: no pinch, no
  two-finger gestures.
- Capture can return empty if the mirroring window is tiny/off-screen or the
  session is paused — that is a connection problem, not a cue to activate and
  thrash the user's desktop or Home Screen.
- **Background capture failed / classic works:** often means stream/permission
  glitch or wrong window id — re-check connection with a screenshot; do not
  spam Home swipes.
