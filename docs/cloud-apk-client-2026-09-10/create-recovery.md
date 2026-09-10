# SDK creation request recovery

September 10, 2026. Candidate, not published. Development API remains on schema
0003 and does not advertise this contract yet. The paired phone-cloud candidate
requires migration 0004 before enabling its request-identity contract.

The SDK negotiates `session_create_idempotency: "v1"` with `/me`, records a
request key before its single creation POST, and retains it on `CreateError` or
the ready `CloudPhone`. CLI output includes the key before the POST. Callers can
save their own key before starting a restartable job. No automatic POST retry or
durable client journal is added. `creation_receipt()` / `cloud receipt` only read;
an explicit same-key, same-parameters creation retry selects the original phone.

The capability check does not apply current rental/quota gates to a retry.
The service still enforces account ownership. Unsupported servers retain the
legacy unkeyed path only when no explicit key was requested, with a CLI notice;
explicit keys fail before POST. Invalid identities, mismatched response keys,
completed requests, changed parameters and foreign-owner receipt reads remain
failures. Attachment by existing session ID never creates a new request.

## Before and after

The [comparison driver](create-recovery-proof.py) loaded baseline SDK source
`26b411f` and the new installed wheel against the same current phone-cloud API
source, real HTTP and disposable Postgres. A loopback proxy consumed the first
successful create response and closed without returning it. Each case then made
one explicit caller retry; only the new case could retain and reuse the identity.
Each case used a fresh isolated account and simulated shlut allocations. The
actual source/wheel hashes and raw results are retained alongside this file.

```
MEASURED case=before create_requests=2 distinct_sessions=2 provider_reservations=2 real_phones=0
MEASURED case=after create_requests=2 distinct_sessions=1 provider_reservations=1 real_phones=0
```

Target: one allocation for a retried logical request. Observed allocation count
2 → 1, a reduction of one (50%); unintended extra allocations 1 → 0. Both cases
made two POST attempts. This is one controlled comparison per condition, not a
latency distribution, real Android capacity result or live deployment proof.
The proxy's loss occurred after the API accepted the first create. It does not
cover every worker allocation failure. All owned simulated phones were removed.

Twenty-one source and isolated installed-wheel tests passed, including the
existing APK/example checks. The new HTTP tests cover lost replies, no automatic
POST retry, receipt reads, explicit replay, completed requests, conflicting
parameters, cross-owner denial, legacy capability handling, invalid keys and
unverified response identity. Wheel and sdist build successfully. CI tests the
installed package on Python 3.10 and 3.13. Both jobs passed for source
`20a78c4960340282ba969f39f0bdb067597e1521` in
[push CI](https://github.com/ShawnPana/phone-harness/actions/runs/34507850902)
and [PR CI](https://github.com/ShawnPana/phone-harness/actions/runs/34507855763).
These results verify the installed client, not a published package or the live
development API's still-undeployed request-identity contract.

The initial CLI subprocess test omitted the checkout import path and inspected
the wrong import environment; its test setup was corrected to the same pattern
as the existing installed-wheel suite. The initial real-API driver omitted the
API source import path and failed before starting services. Both failures and
successful corrected runs are retained. No real accounts, worker phones, shared
database rows or production resources were used.

Saving and later retrieving a request key remains the caller's responsibility
across crashes. Closing a terminal without saved output can lose an automatically
generated key. A 404 receipt does not establish that an earlier in-flight request
cannot arrive later. A completed request needs a new key only when the caller
actually intends a new phone. See [CLOUD.md](../../CLOUD.md) for usage.
