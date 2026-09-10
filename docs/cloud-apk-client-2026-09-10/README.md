# Cloud APK client candidate validation

September 10, 2026. Candidate `0.1.2.dev1`, based on `reland/cloud-backend` at
`46125871`. The account API now has an exact-session APK client command and
Python method. No package was published, no installed user copy was changed,
and these checks did not allocate a real phone.

- Eleven local HTTP tests pass. They verify exact body/hash/credential/target,
  no allocation or retry, unknown receipts, redirect denial, URL transport rules,
  regular-file bounds, FIFO/sparse rejection, pending cleanup and Linux OCR limits.
- Eleven tests also pass against the installed wheel on Linux. The read-only
  container has no external network, 0.5 CPU, 256 MiB RAM and a temporary executable
  mount. Tests and CLI import the installed wheel: no source directory is mounted.
  Installation uses only that local wheel, without downloading dependencies.
- Existing phone-cloud client integration pilots pass: CLI 16, mock 11 checks.
  These use simulated devices and a disposable database.
- Wheel and sdist build. The wheel source and packaged skill match the checkout;
  its manifest records the exact SHA-256. PyObjC dependencies apply only to Darwin.
  The separate publication workflow still requires a version tag; this branch
  does not publish or deploy by being pushed.
- A small native Android QA APK builds, aligns and verifies with v1/v2/v3
  signatures and the expected package/launcher. Its retained build receipt has
  the exact source/toolchain/APK digests. Android has not installed it yet.
  The example runner parses successfully but is awaiting the real paired run.

```
MEASURED client_apk_bytes=95004 existing_session_uploads=1 sessions_created=0 real_phones=0
```

The HTTP fixture is ZIP-prefixed test bytes, not a valid APK. The separate
12,691-byte signed sample is a real package build, not a successful Android
installation. Neither establishes user-visible startup or interaction latency.

An earlier Linux check installed the wheel but could not execute its entrypoint
because the temporary mount was `noexec`. Reconciliation found no running test
container, then the corrected `exec` mount passed. That was a verification
container issue, not a client failure or a hardware performance result.

Remote HTTP URLs were accepted by the previous client. Both JSON and APK calls
now reject remote plaintext, credentials in URLs and redirects before forwarding
account credentials. Literal loopback HTTP remains for local services/tunnels.

Next gate: coordinated paired API/worker deployment, install this APK through a
normal account key, assert actual app behavior, inspect the same phone in the
new browser dashboard, retain screenshots, and confirm release/capacity. The
ordinary startup, browser continuity, six-phone and production-approval gates
remain open.
