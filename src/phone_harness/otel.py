"""OpenTelemetry traces for phone-harness — one span per CLI run, one per helper.

Sits beside telemetry.py (PostHog counts runs) rather than replacing it: this
is the timing view — which helper in a long task was slow, where a run failed.
Spans go to OnePatch over OTLP/HTTP.

Same switch as the rest of the telemetry: off with
`phone-harness config set telemetry false`, or PHONE_HARNESS_TELEMETRY=0 for
one call. Nothing is set up, imported, or sent when it is off.

Deliberately NOT recorded: helper arguments, script text, stdout, OCR text and
exception messages. Everything a phone-harness run touches is the contents of
someone's phone, so the spans carry names, timings and outcomes only — never a
value read off or typed into the screen. See TELEMETRY.md.

Endpoint and token are baked in so an install emits with no setup. The token
is write-only ingest, paired with OnePatch's collector: point
OTEL_EXPORTER_OTLP_ENDPOINT somewhere else and it is NOT sent along, so a
private collector gets the spans with no bearer unless
OTEL_EXPORTER_OTLP_HEADERS supplies one.
"""

from __future__ import annotations

import os
import platform
from contextlib import contextmanager

from . import config as _config
from . import telemetry as _telemetry

ONEPATCH_INGEST_URL = "https://phone-harness.logger.onepatch.dev"
ONEPATCH_INGEST_TOKEN = "op_WXaen7tcATTRBOflkkAhKXvA5_ppxxQHTzTvw3s7MaU"

DEFAULT_SERVICE_NAME = "phone-harness"

# The CLI is short-lived, so spans are flushed at exit. Cap on how long that
# flush may hold up the shell.
DEFAULT_TIMEOUT_SECONDS = 3.0

_provider = None
_tracer = None
_context_token = None


def _parse_headers(raw):
    """The standard comma-separated `k=v` OTLP header env format."""
    if not raw:
        return None
    out = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out or None


def _timeout():
    try:
        return float(os.environ.get("PHONE_HARNESS_OTEL_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


def is_enabled() -> bool:
    return _telemetry.is_enabled()


def _exporter_target():
    """(traces endpoint, headers). The baked-in token authenticates to
    OnePatch's collector and nothing else, so it is bound to the baked-in
    endpoint: override OTEL_EXPORTER_OTLP_ENDPOINT and the Authorization
    header is dropped unless OTEL_EXPORTER_OTLP_HEADERS names a replacement."""
    override = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    endpoint = (override or ONEPATCH_INGEST_URL).rstrip("/")
    headers = _parse_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))
    if headers is None and not override:
        headers = {"Authorization": f"Bearer {ONEPATCH_INGEST_TOKEN}"}
    return f"{endpoint}/v1/traces", headers


def _build_tracer():
    """Import the SDK and stand up a provider. Any failure here — the packages
    not installed, a bad endpoint — leaves tracing off and the CLI unaffected."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    endpoint, headers = _exporter_target()
    timeout = _timeout()
    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME") or DEFAULT_SERVICE_NAME,
        "service.version": _telemetry._version() or "unknown",
        "os.type": (platform.system() or "unknown").lower(),
        "host.arch": platform.machine() or "unknown",
        "process.runtime.version": platform.python_version(),
        # The same anonymous per-machine id PostHog gets; no user identity.
        "phone_harness.install_id": _config.install_id(),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=endpoint,
        headers=headers,
        timeout=max(int(timeout), 1),
    )))
    return provider, provider.get_tracer("phone-harness")


def start(command: str, *, task_length: int | None = None):
    """Begin the run. Returns the root span, or None when tracing is off."""
    global _provider, _tracer, _context_token
    if not is_enabled():
        return None
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace

        _provider, _tracer = _build_tracer()
        span = _tracer.start_span("cli " + command)
        # Make it current so every helper span lands under it as a child.
        _context_token = otel_context.attach(otel_trace.set_span_in_context(span))
        span.set_attribute("phone_harness.command", command)
        client = _telemetry._detect_agent_client()
        if client:
            span.set_attribute("phone_harness.agent_client", client)
        if task_length is not None:
            span.set_attribute("phone_harness.task_length", task_length)
        return span
    except Exception:
        _provider = _tracer = _context_token = None
        return None


@contextmanager
def helper_span(name: str):
    """One CHILD span per helper call. Name only — never the arguments."""
    if _tracer is None:
        yield None
        return
    try:
        # record_exception would attach the message, which on this CLI is
        # screen text; _record_failure records the type instead.
        cm = _tracer.start_as_current_span(
            "helper " + name, record_exception=False, set_status_on_exception=False)
    except Exception:
        yield None
        return
    with cm as span:
        try:
            span.set_attribute("phone_harness.helper", name)
        except Exception:
            pass
        try:
            yield span
        except BaseException as exc:
            _record_failure(span, exc)
            raise


def _record_failure(span, exc: BaseException) -> None:
    """Type of the failure, never its message: exception text on this CLI
    quotes screen contents and typed strings back at you."""
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode
        span.set_attribute("exception.type", type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
    except Exception:
        pass


def finish(span, *, exit_code: int = 0, phone: str | None = None,
           step_count: int | None = None, error_type: str | None = None) -> None:
    """End the root span and flush, bounded by PHONE_HARNESS_OTEL_TIMEOUT."""
    global _provider, _tracer, _context_token
    if span is None:
        return
    try:
        if phone:
            span.set_attribute("phone_harness.phone", phone)
        if step_count is not None:
            span.set_attribute("phone_harness.step_count", step_count)
        span.set_attribute("phone_harness.exit_code", exit_code)
        if exit_code:
            from opentelemetry.trace import Status, StatusCode
            if error_type:
                span.set_attribute("exception.type", error_type)
            span.set_status(Status(StatusCode.ERROR, error_type or "exit"))
        span.end()
    except Exception:
        pass
    try:
        if _context_token is not None:
            from opentelemetry import context as otel_context
            otel_context.detach(_context_token)
    except Exception:
        pass
    try:
        if _provider is not None:
            # Bounded: the shell never waits longer than this for telemetry.
            _provider.force_flush(int(_timeout() * 1000) or 1000)
            _provider.shutdown()
    except Exception:
        pass
    finally:
        _provider = _tracer = _context_token = None
