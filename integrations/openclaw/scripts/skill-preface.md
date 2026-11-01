# phone-harness in OpenClaw

The rest of this skill is the phone-harness guide, written for its command line. This plugin
runs phone-harness VERSION for you, so read that guide like this:

- **Scripts go in the `phone_exec` tool, not a shell.** Where the guide shows
  `phone-harness <<'PY'`, pass what is between the markers as `code`. Do not run `phone-harness`
  through a shell: that skips the approval `phone_exec` asks for and may run another version.
- **The user approves every call**, and sees the script when they do. Do one whole step in each
  script instead of one tap at a time. A denied call did not run, so do not retry it unasked.
- **Every call is a new interpreter.** Save what a later script needs in the agent workspace, the
  folder in `PH_AGENT_WORKSPACE`.
- **Cloud commands run inside a script.** There may be no `phone-harness` on this machine's
  `PATH`. Where the guide shows `phone-harness cloud ...`, call the same command in Python. It
  prints what the command prints and returns its exit code; a script after it drives that phone:

  ```python
  from phone_harness.cloud import cli
  cli(["ls"])
  cli(["start", "ios", "--for", "15m"])
  cli(["start", "ios", "--for", "15m", "--no-watch"])  # without opening the live view
  cli(["stop"])
  ```

- **`phone-harness cloud login` needs the user.** If a script says `Not signed in`, tell them to
  run this in a terminal on the Gateway host, then try again:

  ```bash
  uvx --from phone-harness==VERSION phone-harness cloud login
  ```

- **Ignore the guide's Updates section.** The plugin chooses the phone-harness version.

