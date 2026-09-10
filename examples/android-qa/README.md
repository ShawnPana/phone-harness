# Deterministic Android QA example

This native app gives the cloud workflow observable outcomes: increment and
reset a counter, reject an empty name, and greet a supplied name. It uses no
account, network permission, external backend, or native library. It is a test
fixture, not a claim that arbitrary customer apps are compatible.

Build using an **already installed** Android SDK with platform 35, build-tools
35.0.0 and a JDK with Java 8 bytecode support. The script does not install tools,
start an emulator or contact ADB devices:

```sh
python examples/android-qa/build.py \
  --sdk /path/to/android-sdk --java /path/to/jdk \
  --output /tmp/qa-build/phone-harness-qa.apk
```

The output must not already exist. The script packages the manifest and DEX,
aligns the APK and generates a disposable debug signing key. It verifies the
signature, package ID and launcher before writing the APK and `.build.json`
receipt, then deletes temporary build files and the key. Each build has a
different signature/hash; use the receipt for that exact APK. Use a new temporary
phone for each build, since Android cannot upgrade a package with a different key.

With the candidate client installed and **both server upload endpoints deployed**,
create a temporary shlut session using your normal account API key. Open its
dashboard or watch link, and pass its exact session ID to the runner:

```sh
phone-harness cloud up shlut
python examples/android-qa/verify.py \
  --session SESSION_ID --apk /tmp/qa-build/phone-harness-qa.apk \
  --output /tmp/qa-run-evidence --release
```

The runner never creates a session or changes the selected one. `--release`
authorizes ending that session even if a check fails; use only a session created
for this run. Cleanup is attempted even if the initial attachment fails, and
invalid session IDs are rejected before network access. It retains the installation receipt, checks app state through
Android's accessibility tree, and saves four screenshots. After release it waits
up to 120 seconds for the session to leave the active list. Timeout fails cleanup;
it does not authorize deleting another phone.

The historical `*_input` tap event durations include locating the target through
the hierarchy before issuing its tap RPC. They are complete test-step durations,
not pure input latency. The following accessibility observation is separate.
Accessibility nodes supply their center coordinates; the runner taps those
coordinates directly. Four HTTP example checks cover the complete simulated
app flow, failed input, failed attachment cleanup, and invalid target rejection.
Hierarchy capture is a slow inspection tool, **not** a browser input-to-visible
latency measurement. Browser video/interaction, API readiness, complete startup
and worker capacity/cleanup must also be checked separately. Without `--release`,
the phone remains available for manual testing and cleanup is not claimed.

The first local APK build passed package/signature verification. The corrected
`eb748d0` runner subsequently passed the real development-to-staging sample flow:
install, counter/reset, validation, greeting, four screenshots, and release.
The operator visually inspected the initial and greeting API captures. Browser
viewing of the installed app is still pending. See the client evidence in `docs/`.
