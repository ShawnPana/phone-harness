# Temporary Android app testing

This branch is the **0.1.2.dev1 candidate**, based on the cloud-backend branch
at `46125871`. It adds APK upload and installation through the account-owned
phone-cloud API. It has not been published as a normal customer release. The
matching upload endpoints must be deployed before these install commands work;
the staging worker candidate is deployed, while the development API endpoint
has not been activated. This is not yet a verified real installation path.

## One phone, one build, explicit cleanup

Configure `PHONE_CLOUD_URL` to the intended HTTPS phone-cloud service and
`PHONE_CLOUD_TOKEN` to your account's API key. Use a compatible single APK
(the current staging runtime is ARM64 Android 14). Keep the key in your normal
secret storage; worker credentials and direct ADB access are not needed.
Remote service URLs require HTTPS. HTTP is accepted only for a literal loopback
address such as `127.0.0.1` or `[::1]` for a local service or encrypted SSH tunnel.

The private exe.dev development site also has an ingress sign-in gate. This
client does not borrow browser cookies or replace account authentication with
an operator key. Authorized operators can open an SSH tunnel to the API's
loopback listener, then use their normal application account key:

```sh
ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18723:127.0.0.1:8723 phone-harness-development.exe.xyz
```

Keep that command running and set `PHONE_CLOUD_URL=http://127.0.0.1:18723`
in the client terminal. It binds only to the local machine and requires the
VM's SSH authorization. Keep `PHONE_CLOUD_TOKEN` as the normal account key.
Stop the tunnel with Ctrl-C when testing is finished. Browser viewing still
uses the public HTTPS dashboard/watch URL and requires exe.dev sign-in.
This operator-only development route does not grant SSH access to Web-share
users and is not a customer production setup requirement.

```
phone-harness cloud up shlut
```

The banner prints a session ID and view-only watch link. Export the session ID
as `PHONE_CLOUD_SESSION` and set `PHONE_HARNESS_PLATFORM=cloud`; later helper
scripts then attach to the same phone. Install a local build into that exact
session:

```sh
phone-harness cloud install "$PHONE_CLOUD_SESSION" ./app-debug.apk
```

The command hashes and streams the file with a declared length, then checks
the server's byte-count/hash receipt. It never provisions a new phone, changes
the selected session, follows a redirect, or silently retries installation.
Files must be regular `.apk` files no larger than 256 MiB. AABs and split APK
sets are not supported by this command. A ZIP prefix is only a preliminary
check; Android ultimately validates the package.

In Python, attach explicitly and perform the same upload:

```python
import os
from phone_harness.cloud import CloudPhone

phone = CloudPhone(session_id=os.environ["PHONE_CLOUD_SESSION"])
try:
    receipt = phone.install_apk("./app-debug.apk")
    phone.send("apps.launch", name="com.example.myapp")
    assert phone.send("apps.current") == "com.example.myapp"
    # Add app-specific actions, assertions, and screenshot evidence here.
finally:
    result = phone.release()
    print(result)  # May say cleanup_pending; that is not completed cleanup.
```

For interactive QA with the same exported session, the existing helpers can
launch apps, read accessibility text, tap, type, and save screenshots. Observe
the actual expected app state after each action; an input ACK alone is not a
passed test. The watch link lets the developer see the same phone in a browser.
Use a staging app backend reachable from the cloud phone: your laptop's
`localhost` is not the cloud phone's localhost.

End the session even when a test fails:

```sh
phone-harness cloud down "$PHONE_CLOUD_SESSION"
phone-harness cloud ls
```

The CLI distinguishes completed release from `closing — billing stopped;
cleanup is still pending`. A closing session cannot be attached as a ready
phone. Check that it disappears from the active list before reporting resource
cleanup complete. Temporary cloud sessions do not promise retained app data or
logins for a future session.

## Errors and platform boundaries

An HTTP rejection or receipt mismatch exits the install command nonzero.
`InstallError` carries `status`, `code`, and `outcome` when available. An unknown
outcome (for example, a lost response after Android may have installed) is not
proof of failure: inspect the existing phone before retrying. The command does
not auto-delete it or allocate a replacement.

Cloud commands and `CloudPhone` use standard Python networking and work without
the macOS frameworks. Package metadata installs PyObjC only on macOS. Pixel OCR
through Vision is a macOS capability and is unavailable on other clients;
Android accessibility text and PNG captures remain separate API capabilities.
Some local screenshot geometry helpers still use Vision; this candidate is not
a port of every native helper or iPhone Mirroring feature to Linux.

## Validation and release

The [Android QA example](examples/android-qa/README.md) includes a small native
fixture, its build recipe and a runner that checks app outcomes and saves evidence.

Tests use a real local HTTP server with simulated sessions: exact upload bytes,
owner credential handling, no accidental provisioning, invalid/sparse/FIFO
inputs, bounded streaming, receipt mismatch, redirect denial, and pending
cleanup. They do not install an Android package. The paired real-device
provision/upload/install/launch/assert/browser/evidence/release run is still
required, with provisioning, installation and preview/input timings measured
separately. A package build is not a public SDK release.
