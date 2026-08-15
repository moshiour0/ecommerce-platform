"""
OpenTelemetry setup and async trace propagation (Architecture Rule 6).

Rule 6.4: the trace_id and span_id must be injected into every
OutboxMessage.payload at write time, and consumers must extract them and
create a linked child span. That is the only mechanism by which a trace
survives the synchronous -> asynchronous boundary.

State before this module
------------------------
Nothing was traced at all. Services called FastAPIInstrumentor.instrument_app
but only opentelemetry-api was installed -- not the SDK -- so
get_tracer_provider() returned the no-op provider, every span was
non-recording, and span_context.is_valid was permanently False. The injection
helper in outbox.py would therefore have written nothing even if a service had
called it, and no service did. There was also no exporter and no backend.

Design note
-----------
Injection is wired as a SQLAlchemy before_insert listener rather than a
function each caller must remember. This codebase already demonstrated what
happens to a helper that must be remembered: build_outbox_message existed for
the entire life of the project and was imported by exactly one file. A
listener cannot be forgotten by new code.
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TRACE_ID_FIELD = "_trace_id"
SPAN_ID_FIELD = "_span_id"

_configured = False


def setup_tracing(service_name: str, endpoint: Optional[str] = None) -> None:
    """Install a real TracerProvider with an OTLP exporter.

    Safe to call when the SDK is absent or the collector is unreachable: the
    service degrades to no-op spans rather than failing to start. Observability
    must never be the reason a service cannot serve traffic.
    """
    global _configured
    if _configured:
        return

    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT unset — tracing stays no-op")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        logger.warning("OpenTelemetry SDK not installed (%s) — tracing stays no-op", exc)
        return

    try:
        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
        trace.set_tracer_provider(provider)
        _configured = True
        logger.info("Tracing configured for %s -> %s", service_name, endpoint)
    except Exception as exc:
        logger.error("Tracing setup failed (%s) — continuing without traces", exc)


def current_trace_context() -> Dict[str, str]:
    """Return the active trace/span ids, or {} when nothing is recording."""
    try:
        from opentelemetry import trace
        ctx = trace.get_current_span().get_span_context()
        if not ctx or not ctx.is_valid:
            return {}
        return {
            TRACE_ID_FIELD: format(ctx.trace_id, "032x"),
            SPAN_ID_FIELD: format(ctx.span_id, "016x"),
        }
    except Exception:
        return {}


def inject_trace_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add trace ids to an outbox payload. Returns the same dict for chaining."""
    payload.update(current_trace_context())
    return payload


def enable_outbox_trace_injection(outbox_model) -> None:
    """Inject trace context into every row of this outbox table on insert.

    Registering a mapper listener means the guarantee holds for code that has
    not been written yet, which is the difference between a rule that is
    enforced and a rule that is merely documented.
    """
    try:
        from sqlalchemy import event
    except ImportError:
        return

    @event.listens_for(outbox_model, "before_insert")
    def _inject(mapper, connection, target):  # noqa: ANN001
        ctx = current_trace_context()
        if not ctx:
            return
        payload = dict(target.payload or {})
        payload.update(ctx)
        target.payload = payload


def start_consumer_span(tracer, name: str, payload: Dict[str, Any], **attributes):
    """Continue the producer's trace on the consumer side.

    The span is created as a *child of the producing span in the same trace*,
    not as a separate trace carrying a Link.

    OpenTelemetry's Link is the textbook choice for queue fan-out, and that is
    what this originally used. It was wrong for Rule 6.4, which requires "full
    end-to-end trace continuity across the synchronous -> asynchronous
    boundary". Links produce separate traces: one order was recorded as three
    disconnected traces of 11, 11 and 6 spans, each covering a single hop, so
    following an order end to end still meant stitching trace ids by hand --
    exactly the problem the rule exists to prevent.

    Continuing the trace means the whole lifecycle -- create, reserve, charge,
    confirm -- is one trace keyed by order.

    Falls back to a new root span when the payload carries no trace context
    (commands the reaper emits via raw SQL have none, since they originate in
    a background sweep with no inbound request), so callers need no
    conditional logic.
    """
    attrs = {k: v for k, v in attributes.items() if v is not None}
    try:
        from opentelemetry.trace import (
            NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context,
        )

        raw_trace = payload.get(TRACE_ID_FIELD)
        raw_span = payload.get(SPAN_ID_FIELD)
        if raw_trace and raw_span:
            parent = NonRecordingSpan(SpanContext(
                trace_id=int(raw_trace, 16),
                span_id=int(raw_span, 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            ))
            return tracer.start_as_current_span(
                name, context=set_span_in_context(parent), attributes=attrs
            )
    except Exception:
        pass

    return tracer.start_as_current_span(name, attributes=attrs)


def get_tracer(name: str):
    from opentelemetry import trace
    return trace.get_tracer(name)
