# Changelog

Short summaries of user-visible changes at each release. Tags live in the
[release list](https://github.com/ShawnPana/phone-harness/releases).

## [Unreleased]

- **Setup** – The Fast Path install guide now detects the environment first
  and falls back to a cloud phone when no local phone is available
  ([#138](https://github.com/ShawnPana/phone-harness/pull/138)).
- **Billing** – The docs and skill now state clearly that only a cloud
  Android bills by the minute; a cloud iPhone does not
  ([#137](https://github.com/ShawnPana/phone-harness/pull/137)).
- **Telemetry** – CI builds no longer send usage telemetry; the check now
  respects the `CI` environment variable ([#127](https://github.com/ShawnPana/phone-harness/pull/127),
  [#128](https://github.com/ShawnPana/phone-harness/pull/128)).

## [0.3.2] – 0.3.2

- **Hermes** – Every `phone_exec` call now asks for approval before running a
  script on the phone, and the child process environment is scrubbed of
  secrets ([#125](https://github.com/ShawnPana/phone-harness/pull/125)).
- **Telemetry** – Telecommand off for Hermes runs unless the user explicitly
  opted in ([#124](https://github.com/ShawnPana/phone-harness/pull/124)).
- **Install** – `install.md` documents the uv tool install as the normal
  setup path ([#120](https://github.com/ShawnPana/phone-harness/pull/120)).

## [0.3.1]

- **Cloud** – A dead cloud session is cleared after its error is shown, and
  the screen reader is now `screen()` ([#121](https://github.com/ShawnPana/phone-harness/pull/121)).
- **iOS** – Agents can see a blocked iPhone Mirroring window and react to it
  ([#119](https://github.com/ShawnPana/phone-harness/pull/119)).
- **CLI** – Polished argparse help, fitted `cloud ls` columns, and
  `phone-harness --version` ([#118](https://github.com/ShawnPana/phone-harness/pull/118)).
- **Hermes** – A `phone_exec` plugin in `integrations/hermes` drives the
  phone from Hermes ([#111](https://github.com/ShawnPana/phone-harness/pull/111)).
- **Cloud** – Fixed `cloud ls` rows, `cloud start` wording, and added the
  update check ([#116](https://github.com/ShawnPana/phone-harness/pull/116)).
- **iOS** – Presses Connect once when iPhone Mirroring is paused
  ([#115](https://github.com/ShawnPana/phone-harness/pull/115)).
- **Cloud** – A dead cloud phone no longer falls through to another phone
  ([#117](https://github.com/ShawnPana/phone-harness/pull/117)).
- **Workspace** – The agent workspace is seeded from the wheel without
  overwriting edits ([#114](https://github.com/ShawnPana/phone-harness/pull/114)).
- **Cloud** – Start a phone from the account catalog and drive a cloud
  iPhone over HTTPS ([#105](https://github.com/ShawnPana/phone-harness/pull/105)).

## [0.3.0]

- **Onboarding** – Nothing assumes a waitlist; the flow is the user's own
  phone first, then a demo, then a cloud offer ([#80](https://github.com/ShawnPana/phone-harness/pull/80),
  [#81](https://github.com/ShawnPana/phone-harness/pull/81)).
- **Cloud** – Rent a cloud Android with the first 100 minutes free
  ([#79](https://github.com/ShawnPana/phone-harness/pull/79)).
- **Skill** – The skill starts with which phone to use; cloud status, credit,
  and the waitlist are shown ([#78](https://github.com/ShawnPana/phone-harness/pull/78)).
- **Android** – A never-idle screen is asked once, then read from pixels
  ([#77](https://github.com/ShawnPana/phone-harness/pull/77)).
- **Cloud** – `cloud start` opens the phone's live view; `cloud open` is the
  dashboard ([#76](https://github.com/ShawnPana/phone-harness/pull/76)).
- **Cloud** – Rent a phone from the CLI; Android helpers work as promised
  ([#75](https://github.com/ShawnPana/phone-harness/pull/75)).

## [0.2.1]

- **Cross-platform** – Android works on macOS, Linux, and Windows; iPhone
  needs macOS ([#67](https://github.com/ShawnPana/phone-harness/pull/67)).
- **README** – Simpler intro with a demo; sponsor links added.
- **Docs** – Clarify screenshot coordinate tapping; new
  `setup/` directory for per-platform setup.
- **Misc** – Telemetry, PR template, and verify-loop docs.

## [0.2.0]

- **Android** – Full Android backend via adb ([#39](https://github.com/ShawnPana/phone-harness/pull/39)).
- **Cloud** – Background cloud backend ([#21](https://github.com/ShawnPana/phone-harness/pull/21)).
- **Interactivity** – Interactive input support ([#20](https://github.com/ShawnPana/phone-harness/pull/20)).
- **Transport** – A seamless transport layer between iPhone Mirroring and
  Android ([#19](https://github.com/ShawnPana/phone-harness/pull/19)).
- **iOS** – Reliability fixes for iPhone Mirroring
  ([#18](https://github.com/ShawnPana/phone-harness/pull/18)).
- **Onboarding** – New onboarding documentation
  ([#44](https://github.com/ShawnPana/phone-harness/pull/44)).
- **Docs** – Screenshot coordinate tapping clarified
  ([#45](https://github.com/ShawnPana/phone-harness/pull/45)).

## [0.1.1]

- **Install** – First version that installs from PyPI
  ([#41](https://github.com/ShawnPana/phone-harness/pull/41)).

## [0.1.0]

- **iPhone** – iPhone control via iPhone Mirroring: capture, OCR, taps,
  swipes, and typing ([#14](https://github.com/ShawnPana/phone-harness/pull/14)).