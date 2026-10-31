# Onboarding

You are setting up phone-harness with the user, once. Ask as little as
possible: detect what is already in place, and stop only for the steps that
need the user's hands. Tell them exactly what to do on the phone or in System
Settings, then wait.

## 0. Look before you ask

Check what this machine has: the OS, whether you can run and install
commands, network access, and any phone (`phone-harness doctor`). Then tell
the user in a sentence or two what you can drive from here, for example "This
is a Linux box with no phone attached, so I can use a cloud iPhone or Android."
iPhone Mirroring needs a Mac; without a Mac or a phone, the cloud (step 6) is
the setup. If you cannot install anything, use the API:
https://github.com/ShawnPana/phone-harness/blob/main/setup/api.md

## 1. One question

"Which phone should be your default — your iPhone, an Android on your desk,
or a phone in the cloud (Android or iPhone)?" (Both an iPhone and an Android is fine: set up
each, then ask which is the default.) Someone with no phone at all picks the
cloud: for them, step 6 is the setup and the demo runs there.

## 2. iPhone

Works through the macOS iPhone Mirroring app. Two things only the user can do:

- Pair iPhone Mirroring with the phone once (open the app; the pairing prompts
  need the physical phone).
- Grant **Accessibility** and **Screen Recording** to the app that launches
  your commands (Terminal, iTerm, Cursor, Hermes, or your own agent app).
  macOS does not grant these to the shell.

Check first, and only ask for what is missing:

```bash
phone-harness doctor ios
```

It reads both permissions without prompting or opening System Settings and
names the app to grant (`--json` is the same check for a script). When one
is missing, ask the user to run this in a terminal on the Mac:

```bash
phone-harness doctor ios --fix
```

System Settings opens at the missing list, with a small panel beside it that
shows the app's icon. They drag the icon into the list and confirm with
Touch ID or their password. The panel shows a checkmark when the grant
lands, moves on to the next missing list, then says all set and closes. Not
Now closes it, and it gives up after 10 minutes. From a pipe, which is how
most agents run commands, `--fix` opens the first missing pane and returns
instead of waiting. Over SSH or without a display it prints the steps.
`--yes` and `--non-interactive` never open System Settings and never wait.

If System Settings also lists the Python binary on its own row, turn that on
too. If the switch is already on and the check says `stale`, the app or the
Python binary was updated or re-signed: remove that entry and add it again.
If macOS offers to quit the app after the Screen Recording grant, choose
Later: a new phone-harness process already sees the grant, so the terminal
does not need a restart.

Whenever you capture or verify the screen, bring the Mirroring window forward
so the user can see what you're doing.

## 3. Android

- Install adb (`brew install android-platform-tools`) and, optionally, scrcpy
  (`brew install scrcpy`) for a live mirror during long tasks.
- Ask whether they have a USB cable handy.
  - **USB:** Developer options (Settings → About phone → tap Build number 7×)
    → USB debugging → plug in → tap Allow ("Always allow from this computer").
  - **Wi‑Fi:** Developer options → Wireless debugging (on; same Wi‑Fi as the
    Mac) → tap the row → "Pair device with pairing code" → they read you the
    6 digits → `phone-harness android pair CODE`.
- `phone-harness config set platform android` if it is the default.

Check with `phone-harness android` and `phone-harness --doctor android`.

## 4. Verify

`phone-harness --doctor` for the default phone (add `ios` or `android` to
check the other), then a read-only proof: `screenshot()` and `screen()`,
and read what came back to the user.

## 5. Demo (opt-in)

Ask whether to open phone-harness.com on the phone (Safari on iPhone, Chrome
on Android), tap "Star on GitHub" and star the repo for them — only if they say
yes. If the phone is locked or the session is paused, report the doctor status
instead.

## 6. Then, a cloud Android (offer it once, after the demo)

Phone Harness Cloud rents the user their own Android in the cloud: the same
helpers, nothing to pair, and the phone keeps its apps and logins between
sessions. **New accounts start with $5 of credit — the first 100 minutes are
free.** Only now — after the user's own phone is set up, verified and has
done the demo — offer it once. The order matters: the demo on their own
phone is the point of onboarding; the cloud is the encore.

"Want a cloud Android as well? Same tools, nothing to plug in, and your first
100 minutes are free. It is a one-time sign-in in your browser."

For an iPhone user the pitch is testing on Android without owning one; for an
Android user it is a second, always-available phone. If they say no, note it
in the summary and move on. If yes:

- `phone-harness cloud login` — a URL and a code print, and their browser
  opens. They sign in, or create an account right there if they have none.
  (If sign-ups happen to be on a waitlist at the time, they join it there;
  the sign-in then times out and they come back once invited.) Never click
  Sign up, Join or Approve for them.
- Once signed in: `phone-harness cloud start` (it opens the live view in their
  browser), then the same demo as step 5 on the cloud phone — open
  phone-harness.com in Chrome, offer the star — and `phone-harness cloud
  stop`. Keep it to a few minutes: a cloud Android bills by the minute and the
  credit is theirs. (A cloud iPhone does not bill by the minute.)

## Rules

Never type a PIN. Never change a phone setting without asking. Never sign the
user up for anything or spend their cloud credit beyond the proof. Connecting
the phone is the user's job — relay the doctor's message and wait; don't
retry-loop. After this, day-to-day usage is the phone-harness skill.
Setup reference and troubleshooting:
https://github.com/ShawnPana/phone-harness/blob/main/install.md.
