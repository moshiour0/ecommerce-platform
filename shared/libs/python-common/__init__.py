"""
Shared platform library.

Submodules are exposed lazily (PEP 562). Eagerly importing them here made the
whole package unimportable unless a service also declared PyJWT and
confluent-kafka, because `auth` needs `jwt` and `kafka_client` needs
`confluent_kafka`. A FastAPI service that only wanted `build_outbox_message`
or the retention loop would crash on startup with ModuleNotFoundError.

That is why this library sat unused by every service except stream-processor,
and why outbox construction and idempotency logic were copy-pasted into
fifteen services instead of imported from one place.

With lazy resolution, `from python_common.retention import start_outbox_cleanup`
costs nothing beyond SQLAlchemy, which every service already has. Heavy
dependencies are only imported by the code paths that actually need them.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "verify_jwt",
    "BaseOutboxPayload",
    "build_outbox_message",
    "KafkaAvroConsumer",
    "outbox_cleanup_loop",
    "prune_outbox_once",
    "start_outbox_cleanup",
    "setup_tracing",
    "inject_trace_context",
    "enable_outbox_trace_injection",
    "start_consumer_span",
    "get_tracer",
]

# Public name -> submodule that defines it.
_LAZY_EXPORTS = {
    "verify_jwt": ".auth",                    # requires PyJWT
    "BaseOutboxPayload": ".outbox",
    "build_outbox_message": ".outbox",
    "KafkaAvroConsumer": ".kafka_client",     # requires confluent-kafka
    "outbox_cleanup_loop": ".retention",
    "prune_outbox_once": ".retention",
    "start_outbox_cleanup": ".retention",
    "setup_tracing": ".tracing",
    "inject_trace_context": ".tracing",
    "enable_outbox_trace_injection": ".tracing",
    "start_consumer_span": ".tracing",
    "get_tracer": ".tracing",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_path, __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache so subsequent lookups skip __getattr__
    return value


def __dir__() -> list:
    return sorted(__all__)
