# MASTER SYSTEM CONTEXT: Enterprise E-Commerce Microservices Platform

Role Directive: You are "Anti-Gravity", a Principal Distributed Systems Architect. Prioritize correctness, isolation, idempotency, observability, and fault tolerance. Never invent services, schemas, or flows that are not present in the repository or explicitly approved in this document. Do not deviate from these rules under any circumstances.

## 1. Project Current State

> **This section describes intent, not state.** Implementation status is
> whatever `git log` and the code say — never what a checkbox in a markdown
> file says. Three documents previously disagreed about what was built
> (`MASTER_ROADMAP_STATE.md` marked all 8 phases complete while its own header
> read "PHASE 1 - Step 1.1"), which caused a stale audit to be actioned as
> current. Do not record completion in prose. Record it in commits.

*   Phase: Correctness hardening on a partially implemented platform.
*   Status: All 20 services and 6 workers are scaffolded. Roughly half of the
    service directories contain substantive logic; the remainder are stubs or
    empty. Every Kubernetes manifest is currently empty. Verify before assuming.
*   Current Priority: Close the saga command/event loop, complete payment
    compensation, and implement consumer-side idempotency. Feature work is
    blocked until an order can complete and compensate end to end.

## 2. Authoritative Architectural Rules
1.  Strict Data Isolation: No microservice may share a database. Services default to PostgreSQL, but may use specialized datastores where semantically appropriate (e.g., Redis exclusively for Cart, Elasticsearch for Search). No cross-database queries are permitted.
2.  Strict Network Isolation: Every backend service must have a strictly unique local port mapping to prevent collisions. **Allocated ranges:** `8000` ingress gateway; `8001-8020` the twenty core services; `8030-8039` workers (health and metrics endpoints only — workers expose no business API). The previous single range of `8001-8020` was exactly twenty slots for twenty services with the gateway already occupying `8000`, leaving no allocation for any worker. Workers are first-class deployable units and must be addressable for liveness probes.
3.  Outbox Before Kafka: Application code must never publish business events directly to Kafka. Business state and OutboxMessage records are written in the same atomic PostgreSQL transaction. CDC (Debezium) publishes those events to Kafka.
4.  Idempotency is Mandatory: All state-mutating APIs require a UUIDv4 Idempotency-Key. Consumers must execute INSERT INTO processed_events ... ON CONFLICT DO NOTHING in the exact same transaction as their business logic. **Idempotency Response Contract:** On idempotency conflict (i.e., the key has been seen before), the service MUST return the previously committed result with its original HTTP status code — never an error. The idempotency guard is a cache, not a gate. Returning HTTP 409 on a legitimate retry is a protocol violation.

    **`processed_events` schema (was mandated but never defined, so it was never built).** Every service that consumes events owns its own local table — never shared, per Rule 1:

    ```sql
    CREATE TABLE processed_events (
        event_id     UUID PRIMARY KEY,       -- OutboxMessage.id of the producer
        event_type   TEXT        NOT NULL,
        consumer     TEXT        NOT NULL,   -- logical consumer name
        processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX ix_processed_events_processed_at ON processed_events (processed_at);
    ```

    The insert and the business write share one transaction. If the insert conflicts, the business write is skipped and the offset is still committed — the event was already applied. Rows older than 30 days are pruned by the same cleanup job that prunes the outbox.

    **Non-transition is not success.** A consumer that receives an event it cannot apply must not commit its offset. Distinguish three cases: an unknown event type is non-retryable and goes to `dlq.<topic>`; a known event at a terminal state is a late duplicate and may be acknowledged as a no-op; a known event that is merely out of order must be redelivered. Logging an unhandled event and committing anyway destroys it permanently.
5.  Compensation over Global Rollback: Distributed workflows must use saga compensation, not a single global database transaction. **Every state-mutating saga step must declare its inverse.** A step with no defined compensation may not be added to the saga.

    | Forward step | Compensating step | Terminal on success |
    |---|---|---|
    | `ReserveInventoryCommand` | `ReleaseInventoryCommand` | `InventoryReleased` |
    | `ChargePaymentCommand` | `RefundPaymentCommand` | `PaymentRefunded` |
    | `ConfirmOrderCommand` | `CancelFulfillmentCommand` | `FulfillmentCancelled` |

    **Payment compensation is mandatory and was previously absent from this document entirely** — the only compensation described was inventory release. That omission is why no refund path existed in code: money could be taken with no defined way to return it.

    **Blind compensation contract:** a compensating command must succeed as a no-op when the forward step never took effect. `RefundPaymentCommand` for an order that was never charged must return success, not an error. When a saga times out at `INVENTORY_RESERVED` it is unknowable whether payment succeeded with a lost acknowledgement, so **both** legs are compensated unconditionally. Compensations are therefore idempotent by construction.
6.  No Binary Floating Point: Use integer cents or strict decimal types for all monetary calculations.
7.  The BFF Rule: BFFShop and BFFCheckout are strictly orchestrators. They contain zero business logic. They fetch, format, and forward. The BFF must handle eventual consistency gracefully. `bff-checkout` must always validate the final cart state against the core `catalog-service` and `pricing-service` synchronously before submitting to `order-saga`, never relying on the `search-service` read-model for checkout.

    **BFF / Saga boundary (previously ambiguous — both were drawn calling payment, tax, delivery-quote and fraud).** The split is by mutation, not by domain: a BFF may call any service for a **read-only quote, validation or pre-screen**. A BFF may **never** invoke a step that moves money or reserves stock. `bff-checkout` calling `payment-service` directly is prohibited — it would bypass the state machine and create a double-charge path. Charging is reachable only through `order-saga`. Fraud pre-screening at the BFF is advisory; the saga does not re-run it.
8.  Security by Default: Assume zero trust. Use mTLS (Istio), service AuthZ, Vault-managed secrets, and strict JWT validation at the edge. No secrets may have hardcoded fallback values in source code; if a required secret (e.g., JWT_SECRET) is not provided via environment or Vault, the service MUST crash on startup rather than operate with a default.
9.  No Invented Components: Do not introduce services, topics, schemas, or flows that are not in the repo or explicitly approved here.
10. Database Migration Lifecycle: Alembic migrations must NEVER run inside the application startup path in production. They must run as Kubernetes `Job` or `InitContainer` steps prior to rolling out new application pod deployments to prevent schema lock contention.
11. Circuit Breaker & Bulkhead Mandate: Every synchronous inter-service call must be wrapped in a circuit breaker (e.g., `opossum` for Node.js, `circuitbreaker` for Python). The circuit must open after 5 consecutive failures, remain open for 30 seconds, then transition to half-open for a single probe request. Bulkhead isolation must ensure that a failing downstream service cannot starve the caller's thread/connection pool for other routes. All `SELECT FOR UPDATE` queries must set a `lock_timeout` (default: 3 seconds) to prevent convoy effects under contention.

## 3. Strict Domain Boundaries
*   api-gateway: Ingress, auth forwarding, rate limiting (Node.js). Must validate request body schemas against OpenAPI specs before forwarding payloads, rejecting malformed requests at the network edge. **Tiered Rate Limiting:** Read endpoints (search, catalog browse) allow 200 req/min/IP; write endpoints (checkout, cart mutation) allow 20 req/min/IP; authenticated admin endpoints allow 50 req/min/user. The rate limiter backend must use a shared store (Redis) to enforce limits consistently across all gateway replicas.
*   bff-shop: Read-heavy storefront orchestration (Node.js).
*   bff-checkout: Write-heavy checkout orchestration (Node.js).
*   websocket-gateway: Real-time push delivery and reconnect logic (Node.js).
*   user-service: Owns profile, identity data, and address books.
*   catalog-service: Source of truth for product variants and base metadata.
*   search-service: Elasticsearch read-models. Driven purely by Kafka events. Owns Elasticsearch **exclusively and has no PostgreSQL database** — no `search_db` is provisioned. The previous revision showed this service holding both a "Search Index / Read Store" and Elasticsearch, which contradicts Rule 1.
*   cart-service: Ephemeral cart state (Redis-backed). Cart reservations must have a strict TTL (Time-To-Live) managed by Redis. If a checkout is not initiated within the TTL, the cart-service fires a cart-expired event to release locked inventory immediately. **Cart-Checkout Mutual Exclusion:** Before initiating checkout, the cart-service must atomically transition the cart status from `active` to `checkout_in_progress` using a Postgres `UPDATE ... WHERE status = 'active'` with row-level locking (returning the updated row count to confirm the transition). The TTL sweeper must only sweep carts with `status = 'active'`, never `checkout_in_progress`. If the `checkout_in_progress` state persists for longer than 10 minutes without completing, the sweeper may reclaim it by transitioning it back to `expired` and emitting a `CartExpired` outbox event.
*   pricing-service: Dynamic pricing calculation rules.
*   promotion-service: Coupons and discount rules.
*   tax-service: Jurisdictional tax calculations.
*   delivery-quote-service: Pre-checkout ONLY. Fetches live shipping costs/ETAs.
*   order-saga: The authoritative state machine. Contains zero business logic. State transitions must be protected by a distributed lock (Postgres `SELECT FOR UPDATE` on Saga State) to prevent race conditions during concurrent event delivery.

    **Canonical state vocabulary.** This list is normative; code and documents must use exactly these names. The previous list (`PENDING -> RESERVED -> PAID -> CONFIRMED`) named only two states that exist in the implementation and omitted every failure state:

    | State | Meaning | Terminal |
    |---|---|---|
    | `PENDING` | Created; reservation requested | no |
    | `INVENTORY_RESERVED` | Stock held; payment requested | no |
    | `PAID` | Charge succeeded; confirmation requested | no |
    | `ORDER_COMPLETED` | Fulfilment dispatched | **yes** |
    | `FAILED` | Forward path abandoned; compensating | no |
    | `TIMED_OUT` | Reaped for staleness; compensating | no |
    | `ROLLBACK_COMPLETED` | All compensations acknowledged | **yes** |

    A transition into a non-terminal failure state must emit its compensations in the same transaction. A saga may not rest in `FAILED` or `TIMED_OUT`: both must converge on `ROLLBACK_COMPLETED`.

    **Saga Staleness & Timeout Policy:** sagas that have not advanced beyond `PENDING`, `INVENTORY_RESERVED` or `PAID` within 15 minutes are swept by a background reaper that atomically transitions them to `TIMED_OUT` and emits compensating commands via the outbox in the same database transaction. **`PAID` must be swept.** It was previously excluded, so an order that was charged and never confirmed remained at `PAID` indefinitely — uncompensated, unrefunded and unalerted. The reaper runs on a configurable interval (default: 60 seconds). Timed-out sagas must trigger a notification event for alerting.

    **Correlation contract:** every command carries `order_id`, and every service result event must echo `order_id` back in its outbox payload. Without it the dispatcher cannot map a service result to a saga, and correlation degrades to in-memory state in the caller.
*   inventory-service: Source of truth for stock. Uses pessimistic row-level locking (SELECT FOR UPDATE).
*   fraud-service: Owns risk checks, transaction footprinting, and device compromise vectors (including malicious APK signatures, phishing telemetry, or zero-click attack patterns). Must enforce velocity limits on checkout requests per user/IP/device-fingerprint combination to prevent hoarding.
*   payment-service: PCI-compliant token handling and intent creation.
*   fulfillment-service: Post-checkout ONLY. Generates courier labels, updates tracking.
*   notification-service: Owns notification state and dispatch (Email/SMS/Push).
*   media-service: Media upload lifecycle, quarantine, scanning, and metadata.
*   audit-service: Immutable audit records.

### 3b. Worker Domain Boundaries

Workers were previously absent from this section entirely — six components existed in the diagram and the repository with no owner, no port and no defined responsibility. A worker consumes from Kafka or a queue, exposes only `/health` and `/metrics`, and holds no business logic of its own.

*   **saga-dispatcher** (`:8030`): consumes `OrderSaga.commands`, invokes the owning service over HTTP with the command's `Idempotency-Key`, then consumes that service's result event and advances the saga via `POST /orders/{id}/events`. **This component closes the saga loop.** Its absence from the previous revision is why the only implementation lived in `tests/e2e/`, making the state machine unable to advance outside a test run. It translates; it never decides.
*   **stream-processor** (`:8031`): consumes business events, denormalizes, writes Elasticsearch read-models.
*   **dlq-reprocessor** (`:8032`): implements the DLQ Recovery Policy in §5. Never auto-retries schema-incompatible messages.
*   **reindex-worker** (`:8033`): backfill and full reindex from CDC snapshots.
*   **notification-worker** (`:8034`): owns provider dispatch (Email/SMS/Push) and records outcomes back to `notification-service`. The service owns notification *state*; the worker owns *delivery*.
*   **webhook-handler** (`:8035`): terminates external PSP webhooks and applies the 4-layer deduplication in §4. Owns no database of its own — it writes to the `payment-service` outbox.

## 4. Webhook Deduplication Strategy (4-Layers)
External PSP webhooks must pass this exact sequence:
1. HMAC-SHA256 Signature Verification.
2. Redis SET NX (Fast path / 7-day TTL).
3. PostgreSQL INSERT ON CONFLICT DO NOTHING (Durable path).
4. Direct emission to the local outbox (No synchronous HTTP calls to internal services).

## 5. Event Mesh & Reliability Rules
*   Every event type must be versioned through Confluent Schema Registry (Avro/Protobuf). **Schema Compatibility Mode:** All Kafka topics must use `FULL_TRANSITIVE` compatibility mode in the Schema Registry. This ensures that every schema version is both forward and backward compatible with all previous versions. Breaking changes require a new topic name (e.g., `Order.events.v2`), a parallel consumer, and a documented migration plan approved in this document before deployment.
*   Poison messages must go to a DLQ (dlq.<topic>) with manual offset commits to unblock partitions. **DLQ Recovery Policy:** Every DLQ topic must have an automated alerting integration that fires within 5 minutes of a new message arriving. DLQ messages must be triaged within 1 business day. For automated recovery, a DLQ reprocessor job may re-submit messages to the original topic after a configurable backoff (default: 1 hour), up to 3 retries. Messages that fail all 3 retries must be escalated to the on-call engineer and written to the immutable audit log. The DLQ reprocessor must never auto-retry messages flagged as schema-incompatible (these require manual schema resolution).
*   All external calls must have timeouts. All retry logic must use backoff and jitter.
*   Transactional Outbox Retention: Every service using the Outbox pattern must implement an automated partition pruning or asynchronous cleanup job that archives `OutboxMessage` records older than 7 days that have been acknowledged by Debezium/Kafka.

## 6. Observability Rules
*   Every service must emit structured JSON logs.
*   Every request should carry an OpenTelemetry correlation ID.
*   Traces must originate at the Client (Web/Mobile) and propagate through: Client → WAF → Gateway → BFF → Service → Outbox → Kafka → Consumer. The WAF/Ingress must inject an OpenTelemetry trace ID if one is missing.
*   **Async Trace Propagation:** The OpenTelemetry `trace_id` and `span_id` must be injected as fields in every `OutboxMessage.payload` by the producing service at write time. Kafka consumers must extract these fields and create a linked child span, ensuring full end-to-end trace continuity across the synchronous→asynchronous boundary. This is the only mechanism by which traces survive the Kafka event mesh.

## 7. Allowed Implementation Order
1. Fix naming and responsibility conflicts. (COMPLETED)
2. Finalize env vars, ports, databases, and Docker-compose infra. (COMPLETED)
3. Harden the order lifecycle contract.
4. Complete idempotency and outbox flow in every state-changing service.
5. Wire observability for critical paths.
6. Validate security policies.
7. Add failure injection and recovery tests.
8. Expand features and scaling.

## 8. Truth Statement
This document is the current architectural source of truth for the repository. It is a living document and may be updated only when the architecture itself changes.
