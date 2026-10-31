# Personal iPhone (iPhone Mirroring)

Drives the user's own iPhone through the Mac's iPhone Mirroring window.

## Needs

- A Mac on macOS 15 (Sequoia) or later, with the iPhone Mirroring app.
  Apple's own requirements apply: an iOS 18+ iPhone signed in to the same
  Apple ID, nearby, locked, with Bluetooth and Wi-Fi on.
- Python 3.10+ and the Mac-only dependencies, which the normal install pulls.
- Two macOS permissions for **the app that launches the command** (Terminal,
  iTerm, Cursor, Hermes, or the agent's own app). macOS does not grant them
  to the shell. If System Settings lists the Python binary on its own row,
  that row needs the same grants.
  - **Accessibility** (on macOS 27 and later, the list is named Device
    Control and Data Access): taps and typing.
  - **Screen Recording** (named Screen & System Audio Recording on macOS 15
    and later): seeing the phone.

## Check

`phone-harness doctor ios` reads both permissions as part of the iPhone
check. It does not prompt and does not open System Settings. It names the
app or Python binary to grant and links the exact pane.
`phone-harness doctor ios --json` is the same check for a script.

## Grant

```bash
phone-harness doctor ios --fix
```

In a terminal on the Mac, this opens System Settings at the first missing
list and a small panel beside it with the app's icon. Drag the icon into the
list, then confirm with Touch ID or your password. The panel follows System
Settings when you move it. A new process checks the grant every second; when
it lands, the panel shows a checkmark, moves on to the next missing list,
then says all set and closes. Not Now (or the panel's close button) stops
it, Open Settings brings the list back, and it gives up after 10 minutes.
One guide runs at a time.

Without the panel:

- From a pipe (an agent's shell, or `| cat`), `--fix` opens the first
  missing pane and returns. Run `phone-harness doctor ios` again once the
  switch is on.
- Over SSH or without a display, nothing opens on the Mac. It prints the
  steps and, in a terminal, waits until someone at the Mac grants it.
- When no launching app can be found (tmux, a background service), the
  Python binary is what to grant. `--fix` opens each pane and waits in the
  terminal instead of showing the panel.
- `--yes` and `--non-interactive` never open System Settings and never wait.

If the switch is already on and the state is `stale`, or a new check is
still denied, the app or the Python binary was updated or re-signed. Remove
the old entry with the minus button and add it again. If macOS offers to
quit the app after the Screen Recording grant, choose Later: a new
phone-harness process already sees the grant, so the terminal does not need
a restart.

Manual fallback, if System Settings does not open at the right list (iPhone
Mirroring needs macOS 15, which uses this anchor):

```bash
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
```

## Steps only the user can do

1. Open iPhone Mirroring once and finish pairing (needs the phone in hand).
2. Grant the two permissions above (`phone-harness doctor ios --fix`).

## After that

`phone-harness doctor ios` checks pairing, the mirroring window, capture,
and OCR, and names the first step that is missing. If it passes but taps or
capture do nothing, look for a macOS permission prompt on the Mac.

Not on a Mac, or no iPhone nearby? Use a [cloud phone](cloud.md).
