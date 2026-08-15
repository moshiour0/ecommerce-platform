import uuid
from datetime import datetime, timezone
from typing import Dict, Any
from pydantic import BaseModel, Field

class BaseOutboxPayload(BaseModel):
    id: str
    type: str
    timestamp: str

def build_outbox_message(aggregate_type: str, aggregate_id: str, event_type: str, payload_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Constructs a standardized OutboxMessage dictionary ready for SQLAlchemy insertion.
    Auto-generates IDs and UTC ISO-8601 timestamps.
    Injects OpenTelemetry trace context for async trace propagation (Rule 6.4).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    message_id = str(uuid.uuid4())

    # Ensure payload contains standard event metadata
    if "id" not in payload_dict:
        payload_dict["id"] = message_id
    if "type" not in payload_dict:
        payload_dict["type"] = event_type
    if "timestamp" not in payload_dict:
        payload_dict["timestamp"] = now_iso

    # Async Trace Propagation (Architecture Rule 6.4):
    # Inject trace_id and span_id from OpenTelemetry context into the outbox payload.
    # Kafka consumers extract these to create linked child spans.
    try:
        from opentelemetry import trace
        current_span = trace.get_current_span()
        span_context = current_span.get_span_context()
        if span_context and span_context.is_valid:
            payload_dict["_trace_id"] = format(span_context.trace_id, '032x')
            payload_dict["_span_id"] = format(span_context.span_id, '016x')
    except ImportError:
        pass  # OpenTelemetry not installed — skip trace injection
    except Exception:
        pass  # Trace context unavailable — proceed without it

    return {
        "id": message_id,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "type": event_type,
        "payload": payload_dict,
        "created_at": now_iso
    }
