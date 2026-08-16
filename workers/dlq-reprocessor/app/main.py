"""
dlq-reprocessor (:8032) — implements the DLQ Recovery Policy in §5.

Subscribes to every `dlq.*` topic, and for each message decides one of three
things (the decision itself lives in dlq_rules and is unit tested):

  REPUBLISH  put the original bytes back on the original topic
  ESCALATE   record it in the immutable audit log for a person to triage
  WAIT       the backoff has not elapsed; look again later

Messages are republished as the raw bytes that were originally consumed. The
DLQ router in python_common.kafka_client stores `msg.value()` unchanged, so a
replay is byte-identical to the delivery that failed -- which is the only way
the retry tests the same thing that broke.

On WAIT the offset is deliberately not committed and the message is simply
re-read on a later poll. That is head-of-line blocking on a DLQ partition, and
it is the right trade here: a DLQ is a triage queue measured in messages per
day, not a throughput path, and the alternative -- committing and stashing the
payload somewhere else -- means the durable record of a failed message lives
outside Kafka, which is exactly what a DLQ exists to avoid.

Attempt counts live in Redis keyed by a content fingerprint. They cannot live
in Kafka headers: the DLQ router writes a fresh `error` header and preserves
nothing, so a counter attached to a republished message is lost the moment it
fails again.
"""

import json
import logging
import os
import threading
import time

import redis
import requests
from confluent_kafka import Consumer, KafkaError, Producer
from fastapi import FastAPI
from pythonjsonlogger import jsonlogger

from .dlq_rules import (
    ATTEMPT_TTL_SECONDS, Action, DEFAULT_BACKOFF_SECONDS, MAX_RETRIES,
    attempts_key, is_valid_topic, message_fingerprint, original_topic,
    plan_recovery,
)

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"))
logging.getLogger().handlers = [handler]
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# kafka:29092, not kafka:9092. The broker advertises two listeners:
# PLAINTEXT://kafka:29092 for the mesh and PLAINTEXT_HOST://localhost:9092 for
# the host. A client inside a container that bootstraps on 9092 gets metadata
# naming localhost, then fails to connect to itself -- and confluent-kafka
# reports that as a delivery failure long after produce() returned, so it looks
# like the broker dropped the message rather than like a configuration error.
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "kafka:29092")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
AUDIT_URL = os.getenv("AUDIT_URL", "http://audit-service:8019/audit")
BACKOFF_SECONDS = int(os.getenv("DLQ_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS))
POLL_TIMEOUT = float(os.getenv("POLL_TIMEOUT_SECONDS", "5"))

_stats = {"seen": 0, "republished": 0, "escalated": 0, "waiting": 0, "errors": 0}

app = FastAPI(title="DLQ Reprocessor")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    # Rule 2: workers expose health and metrics only.
    return {"max_retries": MAX_RETRIES, "backoff_seconds": BACKOFF_SECONDS,
            **_stats}


def error_header(msg) -> str:
    for key, value in (msg.headers() or []):
        if key == "error":
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return ""


def escalate(audit_session, msg, payload: bytes, reason: str,
             schema_incompatible: bool) -> bool:
    """Write an escalation to the immutable audit log.

    §5 requires messages that exhaust their retries to be escalated and written
    to the audit log. The audit record is the durable half of that; alerting on
    these records is configured outside this repository.

    The payload is recorded as text with a length cap, not as structured data:
    the reason it is here at all is usually that it could not be parsed.
    """
    body = {
        "actor": "dlq-reprocessor",
        "action": "dlq.escalated",
        "resource_type": "KafkaMessage",
        "resource_id": f"{msg.topic()}:{msg.partition()}:{msg.offset()}",
        "payload": {
            "topic": msg.topic(),
            "partition": msg.partition(),
            "offset": msg.offset(),
            "reason": reason,
            "schema_incompatible": schema_incompatible,
            "error": error_header(msg)[:2000],
            "payload_preview": payload[:512].decode("utf-8", errors="replace"),
            "payload_sha256": message_fingerprint(payload),
        },
    }
    try:
        res = audit_session.post(f"{AUDIT_URL}/records", json=body, timeout=10)
        if res.status_code >= 300:
            logger.error("audit rejected escalation: %s %s",
                         res.status_code, res.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("could not reach audit-service: %s", e)
        return False


def handle(msg, rds, producer, audit_session) -> bool:
    """Process one DLQ message. Returns True when the offset may be committed."""
    payload = msg.value() or b""
    target = original_topic(msg.topic())

    if not is_valid_topic(target):
        # Not a DLQ topic, or a name that cannot be produced to. Escalating is
        # the only safe answer: republishing to the topic it came from would be
        # an unbounded loop at broker speed.
        logger.warning("message on %s has no valid original topic", msg.topic())
        _stats["escalated"] += 1
        return escalate(audit_session, msg, payload,
                        f"no valid original topic for {msg.topic()}", False)

    fingerprint = message_fingerprint(payload)
    key = attempts_key(fingerprint)

    try:
        attempts = int(rds.get(key) or 0)
        last_at = rds.get(f"{key}:at")
        since = (time.time() - float(last_at)) if last_at else None
    except Exception as e:
        # Without the counter this message's history is unknown. Treat it as
        # untried rather than as exhausted: the backoff gate and the audit
        # escalation still bound it, and refusing to act would leave a
        # recoverable message stuck behind a cache outage.
        logger.warning("attempt counter unavailable, treating as new: %s", e)
        attempts, since = 0, None

    decision = plan_recovery(attempts, error_header(msg), since,
                             backoff_seconds=BACKOFF_SECONDS)

    if decision.action is Action.WAIT:
        _stats["waiting"] += 1
        return False  # leave the offset uncommitted; re-read on a later poll

    if decision.action is Action.ESCALATE:
        logger.warning("escalating %s:%s -> %s", msg.topic(), msg.offset(),
                       decision.reason)
        if not escalate(audit_session, msg, payload, decision.reason,
                        decision.schema_incompatible):
            # The audit write is the whole point of escalating. Without it the
            # message would be dropped silently, so the offset stays put.
            _stats["errors"] += 1
            return False
        _stats["escalated"] += 1
        return True

    # REPUBLISH
    try:
        producer.produce(topic=target, value=payload)
        producer.flush(10)
    except Exception as e:
        logger.error("republish to %s failed: %s", target, e)
        _stats["errors"] += 1
        return False

    try:
        rds.set(key, attempts + 1, ex=ATTEMPT_TTL_SECONDS)
        rds.set(f"{key}:at", time.time(), ex=ATTEMPT_TTL_SECONDS)
    except Exception as e:
        # The message is already back on its topic. Losing the counter means a
        # later failure gets a fresh budget, which is a bounded overcount --
        # far better than refusing to commit and republishing it again now.
        logger.warning("could not record attempt for %s: %s", fingerprint[:12], e)

    logger.info("republished %s -> %s (%s)", msg.topic(), target, decision.reason)
    _stats["republished"] += 1
    return True


def consume_forever():
    consumer = Consumer({
        "bootstrap.servers": KAFKA_BROKER,
        "group.id": "dlq-reprocessor",
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    # Regex subscription: every DLQ topic, including ones created after this
    # worker started. Kafka treats a leading ^ as a pattern.
    consumer.subscribe(["^dlq\\..*"])

    producer = Producer({"bootstrap.servers": KAFKA_BROKER})
    rds = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    audit_session = requests.Session()

    logger.info("dlq-reprocessor watching ^dlq.* (backoff %ss, max %s retries)",
                BACKOFF_SECONDS, MAX_RETRIES)

    while True:
        try:
            msg = consumer.poll(POLL_TIMEOUT)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() in (KafkaError._PARTITION_EOF,
                                          KafkaError.UNKNOWN_TOPIC_OR_PART):
                    continue
                logger.error("consumer error: %s", msg.error())
                time.sleep(1)
                continue

            _stats["seen"] += 1
            if handle(msg, rds, producer, audit_session):
                consumer.commit(message=msg, asynchronous=False)
            else:
                # Nothing committed. Pause briefly so a message in its backoff
                # window is not re-read in a tight loop.
                time.sleep(min(POLL_TIMEOUT, 5))
        except Exception:
            _stats["errors"] += 1
            logger.exception("reprocessor loop error; continuing")
            time.sleep(POLL_TIMEOUT)


@app.on_event("startup")
def start_consumer():
    # confluent-kafka's client is blocking and thread-safe, so it runs on its
    # own thread rather than in the event loop, where poll() would stall every
    # health check.
    threading.Thread(target=consume_forever, daemon=True,
                     name="dlq-consumer").start()
