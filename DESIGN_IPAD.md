# iPad Support — Architecture & Implementation Plan

## Problem

phone-harness currently supports iPhone (via iPhone Mirroring) and Android
(via adb). `transport.py` maps `"ipad"` to the iPhone backend, but that
backend drives `com.apple.ScreenContinuity` (iPhone Mirroring.app). macOS
has NO iPad Mirroring application. The backend finds no window and fails.

## Solution: Two-Sided Architecture

Unlike iPhone Mirroring — which provides display AND input in one
application — iPad support needs two separate systems:

| Side    | How                                | Analogous to                         |
|---------|------------------------------------|--------------------------------------|
| Display | QuickTime Player USB mirroring     | `mirror.py` window capture           |
| Input   | WebDriverAgent (XCUITest) over USB | `adb shell input` on Android         |

They are independent, so the iPad backend is an **orchestrator** that owns
one display transport and one input transport, exposes the same `Backend`
vocabulary, and looks like any other phone to `helpers.py`.

```
┌─────────────────────────────────────────┐
│              phone-harness               │
│            (helpers.py unchanged)         │
├─────────────────────────────────────────┤
│           transport.connect()            │
│       platform="ipad" → IPad()          │
├─────────────────────────────────────────┤
│              ipad.py (IPad)              │
│   ┌──────────────┐  ┌────────────────┐  │
│   │  qt_display   │  │   wda_client   │  │
│   │  (QuickTime   │  │ (WebDriverAgent│  │
│   │   window +    │  │  HTTP over     │  │
│   │   OCR)        │  │  USB tunnel)   │  │
│   └──────────────┘  └────────────────┘  │
│         ↕                    ↕           │
│   QuickTime Player    WebDriverAgent     │
│   (macOS window)      (runs on iPad)     │
│         ↕                    ↕           │
│   ┌──────────────────────────────────┐   │
│   │       USB-connected iPad         │   │
│   │  (single cable, always plugged in)│   │
│   └──────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

## New Files

### `src/phone_harness/ipad.py` — IPad Backend

Extends `Backend` and implements every op in the vocabulary:

**Screen ops** delegate to the QuickTime display transport:
- `screen.bounds` — QuickTime window bounds
- `screen.require` — ensures QuickTime is running + iPad camera selected
- `screen.capture` — CGWindowListCreateImage on the QuickTime window
- `screen.text` — Vision OCR on the capture (identical to ios.py)
- `screen.text_pixels` — same (pixels are the only source)

**Input ops** delegate to the WDA HTTP client:
- `input.tap` — `POST /session/:id/wda/tap/:x,:y`
- `input.press` — `POST /session/:id/wda/pressAndHold`
- `input.drag` — `POST /session/:id/wda/dragfromtoforduration`
- `input.scroll` — drag gesture over content-space deltas
- `input.keys` — `POST /session/:id/wda/keys` (single key)
- `input.text` — `POST /session/:id/wda/keys` with the string, or
  character-by-character keystrokes

**Navigation ops** via WDA:
- `nav.home` — `POST /session/:id/wda/homeButton`
- `nav.recents` — `POST /session/:id/wda/appSwitcher`

**App ops** via WDA:
- `apps.launch` — `POST /session/:id/wda/appLauncher` (by bundle id)
- `apps.current` — `GET /session/:id/source` + foreground app detection
- `apps.list` — `GET /session/:id/source` with app listing, or via
  `ideviceinstaller -l` shell command (libimobiledevice)

**Session ops**:
- `session.state` — QuickTime window present + WDA reachable → "ready"
- `session.require` — launches QuickTime, selects iPad camera, verifies
  WDA connectivity, returns bounds; raises with human-readable guidance
  when something is not set up
- `session.detail` — which piece is missing

**Missing by design:**
- `tree` — Unsupported: no accessibility tree over QuickTime capture
  (same as iPhone Mirroring)
- `back` — Unsupported: iPad has no system back button (same as iPhone)
- `raw` — Unsupported: unlike adb, there is no single command channel

**Coordinate space**: like iPhone Mirroring, coordinates are global macOS
screen points of the QuickTime window. `screen.text` boxes carry tap-ready
centers in that space. `tap(x, y)` converts from global screen points
back to iPad-native points for WDA.

### `src/phone_harness/wda.py` — WebDriverAgent HTTP Client

A thin HTTP client for WebDriverAgent's REST API. No dependency on Appium
— talks directly to WDA over HTTP.

```
WDA runs on the iPad (installed once via Xcode).
iproxy (from libimobiledevice) tunnels localhost:8100 → iPad:8100.

wda.py talks to http://localhost:8100 (configurable via env var).
```

Key methods:
- `status()` → bool (WDA running + device connected)
- `create_session()` → session_id
- `tap(x, y)` → POST /session/:id/wda/tap/:x,:y
- `double_tap(x, y)` → POST /session/:id/wda/doubleTap/:x,:y
- `long_press(x, y, duration)` → POST /session/:id/wda/pressAndHold
- `drag(x1, y1, x2, y2, duration)` → POST /session/:id/wda/dragfromtoforduration
- `type_text(s)` → POST /session/:id/wda/keys
- `home()` → POST /session/:id/wda/homeButton
- `app_switcher()` → POST /session/:id/wda/appSwitcher
- `launch_app(bundle_id)` → POST /session/:id/wda/appLauncher
- `screenshot()` → GET /session/:id/screenshot (fallback; QuickTime is primary)

Error handling:
- Connection refused → WDA or iproxy not running → clear error message
- 404 on session ops → session expired → auto-recreate
- HTTP timeouts → device busy / WDA crashed

Coordinate translation:
- WDA operates in iPad-native points (e.g., 1024x1366 for iPad Pro)
- `ipad.py` translates global screen points → iPad points using the
  QuickTime window's video dimensions vs actual iPad resolution
- Same pattern as `image_point()` / `tap_image_point()` in iPhone Mirroring

### `src/phone_harness/qt_display.py` — QuickTime Display Transport

Manages the QuickTime Player window that mirrors the iPad screen.

```
Finds QuickTime Player by bundle ID (com.apple.QuickTimePlayerX).
Finds the "Movie Recording" window.
Captures the movie view within that window.
OCR-able text via Vision framework (same pipeline as ios.py).
```

Key functions (mirror the `mirror.py` interface that `ios.py` expects):
- `running_app()` → NSRunningApplication or None
- `find_window()` → {x, y, w, h, id} or None
- `ensure_window()` → bounds or raise (launches QuickTime if needed)
- `capture(path=None)` → (png_path, bounds) via CGWindowListCreateImage
- `launch()` → opens QuickTime, selects iPad camera
- `show()` → brings QuickTime window to front
- `activate()` → makes QuickTime the active app

QuickTime camera selection via AppleScript:
```applescript
tell application "QuickTime Player"
    set rec to new movie recording
    -- "iPad" is the default camera name; iterate if needed
    set cameras to name of video recording devices
    -- Select the one that contains "iPad"
end tell
```

Alternatively: use `system_profiler SPCameraDataType` to find the iPad
camera, then set it via AppleScript.

**Important discovery**: a QuickTime "Movie Recording" window does NOT
accept HID events as touches on the iPad. Unlike iPhone Mirroring (where a
click in the window IS a touch on the phone), QuickTime is VIEW-ONLY.
That is why WDA is non-negotiable for the input side.

## Changes to Existing Files

### `src/phone_harness/transport.py`

Remove `"ipad"` from the ios/iphone tuple. Add `"ipad"` as its own branch
that imports and returns `IPad`:

```python
if platform in ("ios", "iphone"):
    ...
if platform == "ipad":
    if sys.platform != "darwin":
        raise RuntimeError("iPad control needs macOS (it drives QuickTime + WDA); "
                           "on this machine use Android: ...")
    return importlib.import_module(".ipad", __package__).IPad(**kw)
```

### `src/phone_harness/run.py`

- Add "ipad" to doctor platform docs: `ios|android|ipad`
- Add "ipad" to usage text
- Set `PHONE_HARNESS_DEFAULT_PLATFORM` or config support for iPad

### `src/phone_harness/admin.py`

Add iPad-specific doctor diagnostics:
- Check QuickTime Player.app exists
- Check iPad is connected via USB (`system_profiler SPUSBDataType`)
- Check libimobiledevice tools available (`iproxy`, `idevice_id`)
- Check WDA is reachable at localhost:8100
- Check QuickTime can see iPad as camera source

### `SKILL.md`

Add **iPad** section documenting:
- One-time setup requirements
- The QuickTime + WDA dual-transport model
- How coordinates work (global screen points → iPad points)
- What's unsupported (no back button, no tree, no shell)
- The `ensure_device()` flow

### `install.md`

Add iPad one-time setup section:
1. Install libimobiledevice: `brew install libimobiledevice`
2. Clone and build WebDriverAgent to iPad via Xcode
3. Trust the developer profile on iPad
4. Start iproxy: `iproxy 8100 8100 &`
5. Plug in iPad, open QuickTime, select iPad as camera
6. Run `phone-harness --doctor ipad`

### `integrations/hermes/README.md`

No changes needed — the Hermes plugin calls `transport.connect()` which
picks up the new platform. The `phone_exec` tool already supports any
backend. Users just set `PHONE_HARNESS_PLATFORM=ipad`.

## One-Time Setup (User-Facing)

```
# 1. Install tools
brew install libimobiledevice

# 2. Clone WebDriverAgent
git clone https://github.com/appium/WebDriverAgent.git
cd WebDriverAgent

# 3. Open in Xcode, sign with Apple Developer account
open WebDriverAgent.xcodeproj
# Select WebDriverAgentRunner target
# Signing & Capabilities → your team
# Build to iPad: Product > Test (Cmd+U)

# 4. Trust the profile on iPad
# Settings > General > VPN & Device Management > Trust

# 5. Start the USB tunnel (keep running in background)
iproxy 8100 8100 &

# 6. Configure phone-harness
phone-harness config set platform ipad

# 7. Verify
phone-harness --doctor ipad
```

After setup, the iPad stays plugged in and the agent can use it anytime.

## Daily Use Flow

```
Hermes agent calls phone_exec(code=...)
  → phone-harness transport.connect("ipad")
  → IPad backend
    → session.require:
        - Launch QuickTime if not running
        - Select iPad camera if not selected
        - Verify WDA reachable at localhost:8100
        - Create WDA session if needed
        - Return QuickTime window bounds
    → screen.capture:
        - CGWindowListCreateImage(QuickTime window)
    → screen.text:
        - Vision OCR on capture
    → input.tap(x, y):
        - Convert global screen coords → iPad native points
        - POST to WDA /tap/:x,:y
    → output returns to agent
```

## Risk Areas

1. **QuickTime camera selection reliability**. The iPad camera name varies
   by iPad model. Need robust detection via system_profiler + AppleScript
   fallback.

2. **WDA session stability**. WDA sessions can expire or crash. The client
   needs auto-retry with backoff and clear diagnostics when WDA is
   unreachable.

3. **Coordinate translation accuracy**. Global screen points → iPad native
   points depends on the QuickTime window's video dimensions matching the
   iPad's actual resolution. QuickTime may letterbox or scale. Need to
   measure empirically and calibrate.

4. **WDA keystroke vs paste behavior**. WDA's `/keys` sends character
   sequences that go through iOS autocorrect, same as iPhone Mirroring's
   `keystrokes=True` path. This means "Thu" becomes "thru." The paste
   vs keystrokes distinction from iPhone Mirroring needs an equivalent
   on iPad — possibly via `UIPasteboard` or a custom WDA endpoint.

5. **iproxy tunnel reliability**. If the USB tunnel drops, all WDA
   communication fails. The backend should detect this and report
   clearly rather than timing out silently.

## Implementation Order

Phase 1: Skeleton & Proof of Life
- `wda.py` — HTTP client, status + tap working against real iPad
- `qt_display.py` — window detection + capture working
- Coordinate translation proven accurate under 5px error

Phase 2: Full Backend
- `ipad.py` — all Backend ops wired up
- `transport.py` change — routing
- Doctor diagnostics

Phase 3: Polish
- SKILL.md + install.md updates
- Auto-reconnect on WDA session loss
- Hermes plugin verified working

## Comparison to Existing Backends

| Feature            | iPhone (Mirroring) | Android (adb) | iPad (QuickTime + WDA) |
|--------------------|-------------------|---------------|------------------------|
| Display            | iPhone Mirroring window | adb screencap | QuickTime USB window |
| Input              | HID events into window | adb shell input | WDA HTTP over USB |
| Text recognition   | Vision OCR        | Accessibility tree (primary), OCR fallback | Vision OCR (only) |
| Back button        | Unsupported       | KEYCODE_BACK   | Unsupported |
| App listing        | Unsupported       | pm list packages | WDA source + ideviceinstaller |
| Shell access       | Unsupported       | adb shell       | Unsupported (no adb) |
| Background driving | Yes (SkyLight events) | Yes (adb is always bg) | Yes (WDA is always bg) |
| Coordinate space   | Global screen points | Device pixels | Global screen points → iPad points |