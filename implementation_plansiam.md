# Architecture Design: 9.2 → 10.0 Upgrade Plan

## Why 9.2 and Not 10?

Your blueprint already covers data isolation, outbox-before-Kafka, idempotency, saga compensation, integer-cents money, BFF rules, zero-trust security, and migration lifecycle. That's outstanding. But a true 10/10 distributed systems architecture must also prescribe **what happens when things go wrong at the protocol level** — not just the happy path.

Here are the **8 specific design gaps** that cost 0.8 points, and the exact text I will add to close each one:

---

## Gap 1: No Idempotency Response Contract (-0.15)

**The Problem:** Rule 4 mandates idempotency keys, but never defines what the **response** should be on a retry. Should it return HTTP 409? HTTP 200 with cached result? This ambiguity caused the most dangerous bug in the implementation (Saga returns 409 on retry → customer charged, order never confirmed).

**The Fix:** Add to Rule 4:
> *On idempotency conflict, the service MUST return the previously committed result with its original HTTP status code — never an error. The idempotency guard is a cache, not a gate.*

---

## Gap 2: No Saga Staleness / Timeout Policy (-0.15)

**The Problem:** The architecture defines saga states (`PENDING → RESERVED → PAID → CONFIRMED`) and compensation, but never answers: **How long before a stuck saga is considered dead?** If a Kafka consumer dies, the saga stays in `PENDING` forever and inventory is locked indefinitely.

**The Fix:** Add to order-saga domain boundary (Section 3):
> *Sagas that have not advanced beyond `PENDING` or `INVENTORY_RESERVED` within 15 minutes must be swept by a background reaper that transitions them to `TIMED_OUT` and emits compensating events (e.g., `ReleaseInventoryCommand`). The reaper must also write to the outbox atomically.*

---

## Gap 3: No Circuit Breaker / Bulkhead Policy (-0.10)

**The Problem:** Rule 5.4 says "All external calls must have timeouts. All retry logic must use backoff and jitter." But timeouts alone don't prevent **cascade failures**. If the payment-service is down for 5 minutes, every checkout attempt burns its full timeout budget (3s × 3 retries = 9s per request), exhausting the BFF's connection pool.

**The Fix:** Add new Rule 11:
> *Circuit Breaker Mandate: Every synchronous inter-service call must be wrapped in a circuit breaker (e.g., `opossum` for Node.js, `circuitbreaker` for Python). The circuit must open after 5 consecutive failures, remain open for 30 seconds, then half-open. Bulkhead isolation must ensure that a failing downstream service cannot starve the caller's thread/connection pool for other routes.*

---

## Gap 4: No Cart-Checkout Mutual Exclusion Protocol (-0.10)

**The Problem:** The architecture defines cart TTL and cart-expired events but doesn't define the **coordination protocol** between an in-progress checkout and the TTL sweeper. The sweeper can fire a `CartExpired` event while checkout is actively submitting to the saga, causing inventory to be released immediately after being reserved.

**The Fix:** Add to cart-service domain boundary (Section 3):
> *Before initiating checkout, the cart-service must atomically transition the cart status from `active` to `checkout_in_progress` using a Postgres `UPDATE ... WHERE status = 'active'` with row-level locking. The TTL sweeper must only sweep carts with `status = 'active'`, never `checkout_in_progress`. If the checkout fails or the `checkout_in_progress` state persists for longer than 10 minutes, the sweeper may reclaim it.*

---

## Gap 5: No DLQ Recovery / Reprocessing Policy (-0.10)

**The Problem:** Rule 5.2 says poison messages go to a DLQ, but never answers: **Then what?** Who processes the DLQ? Is it automated? Manual? What's the SLA? Without a recovery policy, the DLQ becomes a graveyard where events die silently.

**The Fix:** Add to Section 5 (Event Mesh & Reliability Rules):
> *DLQ Recovery Policy: Every DLQ topic must have an automated alerting integration that fires within 5 minutes of a new message. DLQ messages must be triaged within 1 business day. For automated recovery, a DLQ reprocessor job may re-submit messages to the original topic after a configurable backoff (default: 1 hour), up to 3 retries. Messages that fail all 3 retries must be escalated to the on-call engineer and written to the immutable audit log.*

---

## Gap 6: No Schema Compatibility Mode (-0.10)

**The Problem:** Rule 5.1 says events must be versioned through Schema Registry, but never specifies the **compatibility mode** (`BACKWARD`, `FORWARD`, `FULL`, `NONE`). Without this, a developer can make a breaking schema change that silently corrupts downstream consumers.

**The Fix:** Add to Section 5 (Event Mesh & Reliability Rules):
> *Schema Compatibility: All Kafka topics must use `FULL_TRANSITIVE` compatibility mode in the Schema Registry. This ensures that every schema version is both forward and backward compatible with all previous versions. Breaking changes require a new topic name (e.g., `Order.events.v2`), a parallel consumer, and a documented migration plan.*

---

## Gap 7: No Correlation ID Propagation Through Async Boundaries (-0.05)

**The Problem:** Rule 6.3 defines trace propagation as `Client → WAF → Gateway → BFF → Service → Outbox → Kafka → Consumer`. But it doesn't mandate **how** the trace ID crosses the Kafka boundary. Without explicit injection into the outbox payload, the trace breaks at the async boundary and you lose end-to-end visibility.

**The Fix:** Add to Section 6 (Observability Rules):
> *Async Trace Propagation: The OpenTelemetry `trace_id` and `span_id` must be injected as fields in every `OutboxMessage.payload` by the producing service. Kafka consumers must extract these fields and create a child span linked to the original trace, ensuring full end-to-end trace continuity across the synchronous→asynchronous boundary.*

---

## Gap 8: No Tiered Rate Limiting Policy (-0.05)

**The Problem:** The gateway's rate limiting is described as a single global rule. But `/api/shop/search` (read-only, public) and `/api/checkout/:user_id` (write, financial) should have vastly different rate limits. A flash sale will legitimately spike search traffic, but checkout attempts should be tightly controlled.

**The Fix:** Add to api-gateway domain boundary (Section 3):
> *Rate limiting must be tiered: read endpoints (search, catalog browse) allow 200 req/min/IP; write endpoints (checkout, cart mutation) allow 20 req/min/IP; authenticated admin endpoints allow 50 req/min/user. The rate limiter backend must use a shared store (Redis) to enforce limits across all gateway replicas.*

---

## Proposed Changes

### [MODIFY] [ARCHITECTURE_STATE_FINAL.md](file:///c:/projects/ecommerce-platform/ARCHITECTURE_STATE_FINAL.md)
- Add Rule 11 (Circuit Breaker / Bulkhead) to Section 2
- Enhance Rule 4 with idempotency response contract
- Enhance order-saga boundary with timeout/reaper policy
- Enhance cart-service boundary with checkout mutual exclusion
- Enhance api-gateway boundary with tiered rate limiting
- Add DLQ Recovery Policy to Section 5
- Add Schema Compatibility Mode to Section 5
- Add Async Trace Propagation to Section 6

### [MODIFY] [mermaid_architecture.md](file:///c:/projects/ecommerce-platform/mermaid_architecture.md)
- Add `SagaReaper` background job node connected to `OrderSaga`
- Add `DLQReprocessor` node connected to `DLQ` and `Kafka`
- Add `CircuitBreaker` annotation on BFF→Service connections

## Verification

After making these changes, I will re-score the architecture against the audit's 5 vectors and confirm every gap is closed.
