# Personal iPhone (iPhone Mirroring)

Drives the user's own iPhone through the Mac's iPhone Mirroring window.

## Needs

- A Mac on macOS 15 (Sequoia) or later, with the iPhone Mirroring app.
  Apple's own requirements apply: an iOS 18+ iPhone signed in to the same
  Apple ID, nearby, locked, with Bluetooth and Wi-Fi on.
- Python 3.10+ and the Mac-only dependencies, which the normal install pulls.
- Two macOS permissions for **the app that launches the command** (Terminal,
  iTerm, Cursor, Hermes, or the agent's own app). macOS does not grant them
  to the shell. If System Settings lists the Python binary on its own, that
  binary needs the same grants.
  - **Accessibility** (on macOS 27 and later, the list is named Device
    Control and Data Access): taps and typing.
  - **Screen Recording**: seeing the phone.

## Check

`phone-harness permissions` prints each permission. It does not prompt and
it does not open System Settings. `phone-harness permissions --json` is the
same report for a script. `phone-harness doctor ios` includes it in the full
iPhone check and does not open Settings either.

When a permission is missing:

```bash
phone-harness permissions --guide
```

That opens the exact Privacy & Security pane and, in a terminal, waits until
the grant lands (10 minutes by default; `--timeout SECONDS` changes it). On
a pipe it opens the pane once and returns, so an agent is not stuck. `--yes`
and `--non-interactive` never open Settings and never wait.

The report names the app or binary to turn on. If it is not in the list,
drag it in. If the toggle is already on and the state is `stale`, or a new
check is still denied, the app or the Python binary was updated or
re-signed. Remove the old entry and grant the one the report names. A new
phone-harness process sees a new grant; do not restart the terminal for the
grant to count.

Manual fallback, if the guide cannot open the pane (iPhone Mirroring needs
macOS 15, which uses this anchor):

```bash
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture"
```

## Steps only the user can do

1. Open iPhone Mirroring once and finish pairing (needs the phone in hand).
2. Grant the two permissions above, for the app `phone-harness permissions` names.

## After that

`phone-harness doctor ios` checks pairing, the mirroring window, capture,
and OCR, and names the first step that is missing. If it passes but taps or
capture do nothing, run `phone-harness permissions` again.

Not on a Mac, or no iPhone nearby? Use a [cloud phone](cloud.md).
