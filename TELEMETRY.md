# Telemetry — phone-harness

## Service
- `service.name`: `phone-harness` · Runtime: Python ≥3.10 (local CLI, one process per invocation)
- Instrumentation: `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-http` — hand-written spans in
  [`src/phone_harness/otel.py`](src/phone_harness/otel.py), wired from
  [`run.py`](src/phone_harness/run.py). No auto-instrumentation packages: the
  harness runs no HTTP server, no DB and no HTTP client, so there is nothing
  for them to patch.
- Transport: OTLP/HTTP protobuf to `https://phone-harness.logger.onepatch.dev/v1/traces`
- Last regenerated: 2026-09-04

## Switching it off
Same switch as the existing PostHog usage events — `telemetry` in the config:

```bash
phone-harness config set telemetry false   # permanent
PHONE_HARNESS_TELEMETRY=0 phone-harness …  # one call
```

When it is off, the SDK is never imported and no span is created.

## Spans

| Span name | Kind | When it fires | Key attributes |
| --- | --- | --- | --- |
| `cli <command>` | INTERNAL (root) | Once per `phone-harness` invocation; `<command>` is one of `script`, `doctor`, `android`, `config`, `skill`, `help`, `usage` | `phone_harness.command`, `phone_harness.phone` (`iphone-mirroring` \| `android`), `phone_harness.step_count`, `phone_harness.exit_code`, `phone_harness.task_length` (character count of the piped script, not its text), `phone_harness.agent_client` (`claude-code`, `codex`, …), `exception.type` on failure |
| `helper <name>` | INTERNAL (child) | Once per helper call inside a piped script — `tap`, `tap_text`, `ocr`, `type_text`, `swipe`, `open_app`, `screenshot`, … (every public callable in `helpers.py`) | `phone_harness.helper`, `exception.type` on failure |

Resource attributes: `service.name`, `service.version` (package version),
`os.type`, `host.arch`, `process.runtime.version`, and
`phone_harness.install_id` — the same random per-machine UUID the PostHog
events already use as their distinct id. No username, hostname, path or
environment variable is attached.

## Metrics / Logs
None. Traces only.

## What is deliberately NOT recorded

Every byte a phone-harness run touches is the contents of someone's phone, so
the review here was stricter than the usual "no PII in attributes" rule and the
answer was to record shapes, never values:

- **Helper arguments.** `type_text("hunter2")`, `tap_text("Reset password")`,
  `open_app(...)` — the argument list is exactly the sensitive part, so
  `helper <name>` carries the helper's name and nothing else. (The existing
  PostHog `cli_event` does capture a truncated argument trace; that is a
  separate, pre-existing destination and this PR did not change it.)
- **Script text and stdout.** The piped script and everything it printed —
  OCR text, screen dumps, message contents — stay out of the spans. Only
  `phone_harness.task_length`, an integer, is recorded.
- **Exception messages.** Recorded as `exception.type` (the class name) only:
  the harness's own errors quote the screen back at you
  (`no text matching '…'`), so the message is screen content. No
  `record_exception()` call, so no stack traces either.
- **URLs, DB statements, request headers and bodies.** Not applicable — the
  harness makes no HTTP or DB calls of its own. The one network call it does
  make (the PostHog usage event) runs in a detached subprocess and is not
  traced.
- **adb / shell command lines.** The Android backend shells out to `adb`; those
  subprocess invocations are not instrumented, so device serials and shell
  arguments are not exported.

## Overhead

The CLI never blocks on the network while it runs: spans batch in memory and
are flushed once at exit, bounded by `PHONE_HARNESS_OTEL_TIMEOUT` (default
`3` seconds). If the SDK is missing or the exporter cannot be built, tracing
silently stays off and the CLI behaves exactly as before.

## Sending it somewhere else

The endpoint and its write-only ingest token are compiled-in defaults so an
install emits with no setup. Standard env vars override:

| Variable | Effect |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Root URL of your own collector. **Setting this drops the built-in `Authorization` header** — the bundled token belongs to OnePatch's collector and is never sent to another one. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Standard `k=v,k=v` list; replaces the built-in header. |
| `OTEL_SERVICE_NAME` | Overrides `phone-harness`. |
| `PHONE_HARNESS_OTEL_TIMEOUT` | Seconds the exit-time flush may take. |
