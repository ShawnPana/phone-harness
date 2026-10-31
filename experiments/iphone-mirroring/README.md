# Native iPhone Mirroring experiment

Opt-in gesture code using consumer wireless iPhone Mirroring. No USB,
pymobiledevice3 or Developer Mode is required. The normal backend and packaged
installation are unchanged; only this directory's launcher loads the adapter.

Requires macOS, a working Mirroring session, the repository's Python
dependencies and Xcode Command Line Tools. Build from the repository root:

```sh
experiments/iphone-mirroring/build
```

Set `PH_MIRRORING_SDK` to a compatible SDK path if needed (the tested compiler
required `/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk`). The launcher
uses `python3`; `PH_MIRRORING_PYTHON` can select your virtualenv's interpreter.

```sh
experiments/iphone-mirroring/run <<'PY'
# task: use the experimental Mirroring input
# step: send Home without front-process preparation and inspect the phone
with no_front():
    press("cmd+1")
print(screen())
PY
```

`no_front()` skips the call that takes Mac keyboard focus. It covers ordinary
tap, long press, swipe and keyboard helpers, plus `double_tap`, `native_nav`,
`native_probe` and `native_raw`. It restores the original behavior on exit;
use it in a single synchronous script. Explicit activation or reconnection
can still take focus. Establish the connection before using it.

Tap, Home-page horizontal swipes, long press, navigation and keystroke typing
worked off-Space without observed focus changes. Use `type_text(...,
keystrokes=True)`; repeated backspaces needed 250 ms gaps. Active-Space behavior
is untested. Outside the context the prepared baseline can steal focus.

The native CLI defaults to a dry run; `--send` posts events:

```sh
experiments/iphone-mirroring/native/mirror_events tap 0.5 0.5 --minimal-record --no-front
```

Ordinary helpers use global Mac screen points; the native CLI uses normalized
full-window coordinates. `norm_point(x, y)` converts between them. Native
magnify/rotate/translate require `--sender ID` with a current local HID service
ID. IDs must not be reused across sessions. Pinch zoom worked with preparation;
no-front pinch is untested. Rotation is unreliable, and scroll/translate remain
unresolved. `scroll()` is disabled to prevent a cursor-warp fallback.

These private APIs are build-dependent. A successful dispatch does not prove
the phone reacted: verify each action on screen. A post-sleep connection once
reported `ready` while showing an interstitial, so check the visible content too.
