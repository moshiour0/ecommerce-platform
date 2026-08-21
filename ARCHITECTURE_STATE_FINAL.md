# MASTER SYSTEM CONTEXT: Multi-Vendor Marketplace (Bangladesh, COD-first)

Role Directive: You are "Anti-Gravity", a Principal Distributed Systems Architect. Prioritize correctness, isolation, idempotency, observability, and fault tolerance. Never invent services, schemas, or flows that are not present in the repository or explicitly approved in this document. Do not deviate from these rules under any circumstances.

## 1. Project Current State

> **This section describes intent, not state.** Implementation status is
> whatever `git log` and the code say — never what a checkbox in a markdown
> file says. Three documents previously disagreed about what was built
> (`MASTER_ROADMAP_STATE.md` marked all 8 phases complete while its own header
> read "PHASE 1 - Step 1.1"), which caused a stale audit to be actioned as
> current. Do not record completion in prose. Record it in commits.

*   Phase: Marketplace build-out on a hardened single-tenant core.
*   Product: A multi-vendor marketplace for **Bangladesh**, modelled on Daraz
    rather than on Alibaba's B2B flow. Many sellers, one buyer cart, one
    checkout, orders split per seller. Decided 2026-08-21; see
    MARKETPLACE_ROADMAP.md §8, which is the authority on *why* and is not
    restated here.
*   **Cash on delivery is the primary payment path.** This is a structural
    decision, not a payment option — see §3d. Card is secondary.
*   The intended differentiator is the **Media Center** (§3c): AI try-on, 3D
    view, a social feed. Reserved, seamed, deliberately not built.
*   Team: 10 engineers, which is what the roadmap's phasing assumes.
*   Status: 21 services and 6 workers are scaffolded; roughly half the service
    directories contain substantive logic. Every Kubernetes manifest under
    `infrastructure/k8s/services` is currently empty. Verify before assuming.
*   Current Priority: the marketplace path. Seller permission is enforced on
    listing, orders split per seller, the COD lifecycle runs from confirmation
    to settlement, couriers sit behind one contract with their remittances
    reconciled, and the escrow ledger records what each seller is owed from
    the moment a courier collects (§3d, §3e). Next is `bff-seller`, so sellers
    can see any of it without reaching internal services. The single-tenant
    order loop (saga, compensation, consumer idempotency) is closed and proven
    by `tests/e2e`.

## 2. Authoritative Architectural Rules
1.  Strict Data Isolation: No microservice may share a database. Services default to PostgreSQL, but may use specialized datastores where semantically appropriate (e.g., Redis exclusively for Cart, Elasticsearch for Search). No cross-database queries are permitted.
2.  Strict Network Isolation: Every backend service must have a strictly unique local port mapping to prevent collisions. **Allocated ranges:** `8000` ingress gateway; `8001-8020` the twenty core services; `8030-8039` workers **`8001-8020` is now full** — seller-service took `8020` on 2026-08-21. The next core service needs this rule amended, and the free extension is `8021-8029`, since `8030-8039` is workers and `8040-8049` is reserved for Media Center. (health and metrics endpoints only — workers expose no business API). The previous single range of `8001-8020` was exactly twenty slots for twenty services with the gateway already occupying `8000`, leaving no allocation for any worker. Workers are first-class deployable units and must be addressable for liveness probes.
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

    **Under COD the table above does not apply, because there is nothing to refund.** No money moves at checkout, so the compensating step for a cancelled COD order is not a refund — it is releasing stock, and after dispatch it is a return-to-origin that costs the platform shipping in both directions. The COD forward path and its inverses are in §3d. Both tables are normative; which one applies is decided by the order's payment method and by nothing else.

    **Blind compensation contract:** a compensating command must succeed as a no-op when the forward step never took effect. `RefundPaymentCommand` for an order that was never charged must return success, not an error. When a saga times out at `INVENTORY_RESERVED` it is unknowable whether payment succeeded with a lost acknowledgement, so **both** legs are compensated unconditionally. Compensations are therefore idempotent by construction.
6.  No Binary Floating Point: Use integer minor units or strict decimal types for all monetary calculations. **The minor unit is the poisha** (1 BDT = 100 poisha), so the existing `*_cents` columns and fields are correct in arithmetic and wrong in name; they are not renamed, because a rename across every service, event schema and read model buys nothing that a comment does not. Read `_cents` as "minor units of the order's currency".

    Multi-currency must stay *possible* without being built: cross-border sourcing is the obvious later expansion, and a schema that hardcodes BDT would have to be migrated with live orders in it. Every monetary amount therefore travels with an explicit `currency` field defaulting to `BDT`, and no service may assume the default.
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

    **Under COD the primary score is refusal risk, not card fraud.** There is no stolen card to detect at checkout, because no card is presented. The loss is a **return-to-origin**: goods dispatched, refused at the door, shipping paid twice and the item restocked or damaged. The signals are different in kind — address completeness and deliverability, prior refusals against this phone number or address, order value against the district's norm, category, and whether the buyer answers the courier's confirmation call. Card-fraud scoring stays for the secondary card path; it is not the default any more.
*   payment-service: PCI-compliant token handling and intent creation, and the **escrow ledger** (§3d) — double-entry, append-only, the record of what the platform owes each seller between delivery and payout.
*   fulfillment-service: Post-checkout ONLY. Owns shipments and the one internal contract every courier sits behind (§3d): status mapping from `config/couriers/*.json`, callback ingestion, and settlement reconciliation. Never per-provider logic outside that mapping.
*   notification-service: Owns notification state and dispatch (Email/SMS/Push).
*   seller-service: Seller onboarding, KYC document references, versioned commission contracts, and seller status. **The authority on whether a seller may sell** — see §4b. Owns `seller_db`. Holds no document contents and no payout account numbers.
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

### 3c. Media Center (reserved, not built)

Media Center — AI try-on and a social feed — is planned for later
(MARKETPLACE_ROADMAP.md §3.6). Nothing here is implemented, and nothing emits
these events. They are declared now so that building it later is additive
rather than a migration.

**Ports 8040–8049 are reserved** for Media Center services and must not be
allocated to anything else. Not 8020–8029: Rule 2 gives `8001-8020` to the
core services, so that range overlaps the last core slot -- exactly the
collision Rule 2 exists to prevent. Workers hold `8030-8039`, so Media
Center starts at 8040.

**Bounded context.** Media Center gets its own database and its own services.
Commerce may **never** call it synchronously: a product page that cannot render
because a social feed is slow is a self-inflicted outage. Integration is by
events in both directions, and neither side blocks on the other.

**Asset purposes** (implemented, migration 013). Every media asset declares
what it is for, because that decides who may read it:

| Purpose | Confidential | Notes |
|---|---|---|
| `product_image` | no | public once scanned clean |
| `post_media` | no | shared to the feed deliberately |
| `seller_document` | **yes** | KYC, trade licence, bank details |
| `try_on_source` | **yes** | a photograph of a person's body |

`try_on_source` is confidential for a reason worth stating: it is the most
sensitive content this platform will hold, and it is indistinguishable from a
product photo by file type. Scanning it clean does not make it public. An
unknown purpose is treated as confidential, so a new kind of upload cannot
become public by being forgotten.

**Object storage** (implemented). The purpose is not advisory — it picks the
key prefix, and the prefix is what the bucket policy is written against:

| Purpose | Key prefix | Anonymous read |
|---|---|---|
| `product_image` | `products/` | allowed |
| `post_media` | `posts/` | allowed |
| `seller_document` | `documents/` | **denied** |
| `try_on_source` | `try-on/` | **denied** |

The policy is generated by `media_rules.bucket_policy()` from that same
taxonomy and applied by `scripts/bootstrap_storage.py`, so adding a purpose
cannot leave the bucket disagreeing with the service about who may read it.
Confidential prefixes appear in no statement at all: they are denied by S3's
default, not by a rule that could be edited away. `bootstrap_storage.py
--verify` reports drift, and `tests/integration/storage_policy.sh` asks the
bucket itself, in CI.

**Bytes never transit a service.** `POST /media/{id}/upload-url` returns a
pre-signed PUT and the client uploads directly to the store. Two constraints
carry the weight:

- The URL is issued **only while the asset is quarantined**. Re-issuing after
  a clean verdict would let the bytes at that key be replaced with content the
  scanner never saw — the one way to defeat quarantine from outside.
- The **content type is part of the signature**. A URL obtained for a JPEG
  cannot be used to store an executable; the store rejects the mismatch.

**Event contracts.** Declared, not emitted. The first three exist today; the
rest are the seam:

```
MediaRegistered      media_id, owner_id, purpose, content_type, filename   (emitted)
MediaPublished       media_id, owner_id, purpose, status                   (emitted)
MediaQuarantined     media_id, owner_id, purpose, status                   (emitted)

TryOnRequested       request_id, user_id, source_media_id, product_id
TryOnCompleted       request_id, source_media_id, result_media_id,
                     product_id, fit_metadata
TryOnFailed          request_id, source_media_id, reason

PostCreated          post_id, user_id, media_ids[], product_ids[], caption
PostEngaged          post_id, user_id, kind (like|comment|share)
PostRemoved          post_id, moderator_id, reason
```

Try-on is an **asynchronous job pipeline**: upload, queue, GPU worker, result
stored as a new media asset with purpose `post_media` or a new result purpose,
user notified. It is a print job, not a request/response.

The social feed is **fan-out on read** until there is a measured reason to do
otherwise. Fan-out on write is an optimisation with a large operational cost,
and building it before the feed has users is speculative.

### 3d. The marketplace model: split orders and cash on delivery

Declared here, mostly not built. This is the contract the implementation must
follow, in the same spirit as §3c — writing it down first is what stops two
services inventing incompatible halves of it.

**One cart, many sellers, many orders.** A buyer fills one cart from any number
of shops and checks out once. What comes out is one `Order` the buyer sees and
one `SellerOrder` per seller, because everything after checkout is per-seller:
stock comes from that seller, the courier collects from that seller's address,
the money is owed to that seller, and a refused delivery is that seller's
return.

```
Cart (buyer)  ──checkout──▶  Order (one, buyer-facing: total, address, status)
                                 │
                                 ├── SellerOrder A  (seller A's lines, courier, payout)
                                 └── SellerOrder B  (seller B's lines, courier, payout)
```

The buyer-facing `Order` status is **derived** from its SellerOrders and never
stored as an independent truth. Two sources for "is this order delivered" is
two answers the first time a courier is late.

**Cash on delivery inverts the lifecycle.** Under a card flow the money is
captured before fulfilment and the risk is a chargeback. Under COD the money
arrives days later from a courier's settlement, and the risk is that nobody
answers the door.

| | Card path (secondary) | COD path (primary) |
|---|---|---|
| Money moves | at checkout | at delivery |
| The saga waits on | a PSP webhook | a courier settlement file |
| Inventory held for | minutes | days |
| Loss vector | chargeback | **return-to-origin** |
| Seller payout follows | PSP payout schedule | courier remittance, reconciled |

**COD state vocabulary** (normative, and distinct from the card vocabulary in
§3 — a SellerOrder is in exactly one of these):

| State | Meaning | Terminal |
|---|---|---|
| `PENDING` | Created; reservation requested | no |
| `INVENTORY_RESERVED` | Stock held for this seller's lines | no |
| `CONFIRMED` | Accepted by the seller; awaiting dispatch | no |
| `DISPATCHED` | Handed to a courier | no |
| `DELIVERED` | Buyer took it and paid the courier | no |
| `SETTLED` | Courier remitted; seller payable | **yes** |
| `RTO_IN_TRANSIT` | Refused or undeliverable; coming back | no |
| `RETURNED` | Back with the seller; stock restored | **yes** |
| `CANCELLED` | Ended before dispatch; stock released | **yes** |

`DELIVERED` is deliberately **not** terminal. The order is complete for the
buyer and unfinished for the platform: the cash is with the courier, and until
it is remitted and reconciled the seller cannot be paid. Treating delivery as
the end is how a marketplace loses track of money it is holding.

**Compensation under COD** (Rule 5's inverse table, for this path):

| Forward step | Compensating step | Notes |
|---|---|---|
| `ReserveInventoryCommand` | `ReleaseInventoryCommand` | as the card path |
| `ConfirmSellerOrderCommand` | `CancelSellerOrderCommand` | only before dispatch |
| `DispatchCommand` | *none* — an RTO, not a rollback | goods must physically return |

The third row is the important one. **After dispatch there is no compensation,
only a return.** A saga cannot undo a van. `RTO_IN_TRANSIT` is a forward state
that happens to end where it started, and the shipping is spent either way —
which is exactly why refusal risk is scored before dispatch, not after.

**The reservation TTL cannot be the card TTL.** Stock is held from checkout to
delivery, which is days. A 15-minute saga reaper (§3) applied to a COD order
would release stock under an order already on a van. The reaper's timeout is
therefore per-path, and the COD path's staleness checks are per-state — a
`CONFIRMED` order that has not been dispatched in 48 hours is a seller
problem and an alert, not an expiry.

**Escrow (implemented).** Money collected by a courier belongs to the seller
minus commission, and the platform holds it in between. That is a liability, so
it is a ledger entry from the moment of delivery — not a number computed at
payout time from orders. A derived number answers "what do we think we owe" and
cannot answer "what did we owe last Tuesday", "why does this disagree with the
orders", or "which of the two is wrong". A ledger answers all three, because
every change is an entry with a reason attached.

Four accounts, double entry, debits positive:

| Account | | Delivery | Settlement | Payout |
|---|---|---|---|---|
| `COURIER_RECEIVABLE` | asset | +collected | −remitted | |
| `SELLER_PAYABLE` | liability | −(collected−commission) | | +paid |
| `COMMISSION_REVENUE` | income | −commission | | |
| `CASH` | asset | | +remitted | −paid |

A transaction balances when its entries sum to zero, and the whole ledger is
consistent when every entry ever written sums to zero — one query, and the only
check that matters. `GET /escrow/health` runs it and reports the imbalance as a
number, because the number is the size of the problem. Unbalanced entry sets
are refused before they are stored: money appearing from nowhere is worse than
a rejected request, since the rejection is loud and the imbalance is silent
until somebody reconciles a bank statement.

**Rounding: one side is computed and the other derived.** `commission = amount
* rate // 10000` and `seller = amount - commission`. Computing both from the
rate independently loses or gains a unit whenever the percentage does not
divide exactly, which is most orders. Deriving the second side makes the
identity true by construction. Flooring rounds in the seller's favour, which is
a policy choice and the right way round — a marketplace that rounds fractions
towards itself is doing something it would not want to explain.

**The rate comes from the contract the seller accepted**, not the current one.
`seller_rules.COMMISSION_BPS_BY_VERSION` keeps every version forever, because a
seller who accepted version 1 is owed version 1's rate on every order placed
under it. Reading it at delivery rather than snapshotting at checkout is safe
for that reason: the rate only moves when the seller deliberately accepts new
terms, and `may_list_products` already stops them selling until they do. The
rate and version used are written onto every entry regardless — the ledger is
the evidence, and it has to stand on its own.

Booking fails closed. A commission that cannot be established is never
defaulted to the current rate: money booked against a contract nobody can
produce is exactly the number that survives until a seller disputes it.

The ledger is **append only**. No update path, no delete path; a correction is
a new transaction reversing the old one, because a ledger that can be edited is
one nobody can testify from. Delivery bookings are unique on
`(seller_order_id, reason, account)`, which makes at-least-once delivery and
courier resends a no-op rather than a second credit.

Settlement moves money between two platform assets and deliberately does not
touch what the seller is owed — that was decided at delivery, and a slow
courier is the platform's problem. Whether a *short* remittance should be
booked at all is `fulfillment-service`'s reconciliation decision (§3d above);
hiding a shortfall inside a balanced pair of entries here would be the wrong
place to catch it.

**Not built:** nothing executes a payout. `POST /escrow/payout` records that
one happened and clears the liability; actually moving money to a seller's bank
account needs the payout rails, the account details that onboarding
deliberately does not store (§3e), and an approval step. A payout run that
selects which sellers to pay is also absent — the balance query it would be
built on is there.

**Couriers are an external integration with many providers, behind one
contract (implemented).** Pathao, Steadfast, RedX, Sundarban and the rest each
have their own API, their own status vocabulary and their own settlement
format. They sit behind one contract in `fulfillment-service`, exactly as the
PSP does behind `payment-service` — never with per-provider logic in the saga.

The canonical vocabulary is the platform's and is fixed. Each provider's words
map onto it from `config/couriers/*.json`, mounted rather than baked into the
image, so the tenth courier is a file rather than a release. Four canonical
statuses drive the seller order and the rest are recorded and change nothing:

| Canonical | Drives | Why |
|---|---|---|
| `PICKED_UP` | `dispatch` | the seller has handed it over |
| `DELIVERED` | `deliver` | consumes stock; the buyer paid the courier |
| `RETURNING` | `mark_rto` | the courier has given up |
| `RETURNED` | `complete_return` | back on the shelf |
| `DELIVERY_FAILED` | *nothing* | couriers retry two or three times before giving up; treating the first failure as a return sends stock back for a buyer who was simply out |

**An unknown status is never guessed.** A word the mapping has not been taught
returns nothing, is stored verbatim on the shipment, flagged, and emitted as
`ShipmentStatusUnmapped` for a human. Both defaults are worse: `IN_TRANSIT`
hides a delivery, `DELIVERED` consumes stock on a word nobody has read.
Couriers add statuses without announcing them, and the first symptom is
normally a parcel stuck in a state nobody recognises.

**A mapping that cannot see a delivery is refused before it is used.**
`validate_mapping` requires the four load-bearing statuses, and the service
refuses to start on a broken file. A courier whose config omits `DELIVERED`
accepts every callback politely and moves nothing — parcels sit dispatched
forever while sellers wait to be paid, and no error appears anywhere. A
service that will not start is a short outage; one that runs blind is a week of
unpaid sellers nobody has noticed.

**Settlement is reconciled, not believed.** The courier's remittance file is
the only evidence the platform has that it was paid. Every row is classified —
matched, short, over, unknown order, not delivered, duplicate, invalid — and
every row is stored, including the ones that did not reconcile: a rejected row
that is forgotten is a dispute nobody can reconstruct. Nothing in that path
adjusts anything, and the variance is not netted across rows, because a file
short on one order and over on another is two problems rather than a balanced
one.

A resent batch is a no-op rather than a second payout, enforced by uniqueness
on `(provider, batch_reference, row_reference)` and by refusing to settle a
seller order that already has a matched row.

**Not built:** no real provider is configured. `config/couriers/manual.json`
is the operations-uploads-a-spreadsheet courier, which is a real thing in this
market and a worked example of the format. Pathao, Steadfast and RedX each
need a file written from *their* documentation — deliberately absent rather
than present with plausible-looking values, because a mapping written from
memory is a guess that will be reviewed as data and trusted as data.

**The split is implemented.** Checkout writes one `Order`, one `SellerOrder`
per seller and one `order_line` per line (migration 016), and emits one
`SellerOrderCreated` per seller order. `GET /orders/{id}/seller-orders` returns
the breakdown together with the derived parent status.

`seller_id` reaches order-saga because bff-checkout puts it there: it already
fetches every product from catalog to validate the cart (Rule 7), so the
seller is one field on a response it was reading anyway. A line that arrives
without one **refuses the whole checkout** rather than splitting the part that
parses — a line nobody can be paid for is a line nobody can be asked to ship,
and creating the other seller orders would leave it in an order that can never
complete, with stock reserved against it.

Order lines had no home before this. They existed only inside the outbox
payload on the way to the inventory reservation, so `order_db` knew a total
and not what it was a total of. That was survivable for one tenant and is not
survivable for a marketplace, where courier collection, payout, commission,
returns and seller metrics are all per seller and none of them can be answered
from a total.

**The lifecycle is implemented.** `POST
/orders/{id}/seller-orders/{sid}/{action}` drives a seller order through the
table above — one endpoint per named action rather than a PATCH taking a
target status, because a caller that can name the destination can name *any*
destination, and the guard then lives in whatever validates the field.

**A COD order is never charged.** `resolve()` takes the order's payment
method, and on the COD path `InventoryReserved` completes the saga with no
command instead of emitting `ChargePaymentCommand`. That arm of the card table
was not merely unnecessary under COD — it charged a card that was never
presented, for goods nobody had received. `ORDER_COMPLETED` on a COD saga
means "the saga finished its work", not "the buyer has their goods"; those are
the same statement for a card order and days apart for a COD one, which is
precisely why the buyer-facing status is derived from the seller orders rather
than read from that column.

**The reaper no longer sweeps COD orders that hold stock.** Fifteen minutes is
right for a card order, which completes in seconds, and catastrophic for a COD
one, which holds stock from checkout until delivery — it would release the
stock out from under an order already on a van, and then the goods arrive, the
buyer pays a courier, and nothing records that anybody is owed anything. COD
orders are still swept at `PENDING`, where a reservation that never came back
really is broken.

**Delivery consumes stock; nothing else does.** This closed a real leak. Before
it, `reserve` moved units from available to reserved and nothing ever moved
them out: a delivered order's reservation stayed `held` forever, and
`quantity_reserved` only grew — 41 units across the platform were held by
orders that had long since completed. `plan_consume` is the counterpart to
`plan_release`, and the asymmetry is the whole point:

| Ending | available | reserved | total |
|---|---|---|---|
| delivered | unchanged | −N | **falls** — the buyer has the goods |
| returned | +N | −N | conserved |
| cancelled | +N | −N | conserved |

A dispatched parcel is still returnable, so it stays reserved. Consuming at
dispatch would lose everything that comes back; releasing on delivery would
put sold goods back on sale.

**Still not built:** refusal-risk scoring in `fraud-service` before dispatch.
The signals are named in §3, but a scoring model invented here would be a
guess with a number attached. There is no courier adapter either (§7 step 11):
`courier_name` and `tracking_code` are free text supplied at dispatch, and
fixing their shape before reading a real provider's API would fix the wrong
shape. And no escrow ledger (step 12) — `SETTLED` records that a courier
remitted, but nothing yet records what the platform owes the seller.

Tax, shipping and promotions are **not** allocated across sellers. That
allocation decides what each seller is paid and what commission is charged on,
so it is a finance decision rather than an arithmetic one. What is guaranteed
is conservation: the seller subtotals sum to the order's goods subtotal
exactly, asserted in both the unit tests and `tests/e2e/test_12`.

### 3e. Seller onboarding (implemented)

`seller-service` on port 8020, owning `seller_db`. migrations/012 gave every
product a `seller_id` and said plainly that the id pointed at a service that
did not exist yet. This is that service.

**One state, not a set of booleans.** `is_verified` / `is_active` /
`is_banned` produces combinations nobody designed — verified and banned,
active but unverified — so onboarding is a single status with an explicit
transition table:

```
registered → documents_submitted → under_review → approved → active
                    ↑                    │                      ↕
                    └──── rejected ──────┘                  suspended

                    banned  (terminal, reachable from anywhere)
```

**The invariant:** `may_list_products` is a whitelist of exactly one status
plus one condition — ACTIVE, and the accepted contract version equal to the
current one. Every unrecognised status answers False, so a row written by a
migration or a future version cannot become permission by accident. This is
the same shape as `media_rules.is_servable`, for the same reason.

| Distinction | Why it exists |
|---|---|
| APPROVED vs ACTIVE | KYC passing is not agreement to commission terms. Keeping them apart makes "terms changed" a version bump rather than a re-run of KYC |
| REJECTED vs BANNED | Most rejections are a badly-lit photograph; making that terminal is a support ticket each time. A decision not to want this seller at all is a different decision |
| suspend vs ban | Suspension is reversible and returns to ACTIVE without a second review |

**What this service does not hold.** Nothing from inside a KYC document — no
national ID number, no licence number, no bank account number. Documents are
opaque `media_id` references to assets registered with media-service under
`purpose=seller_document`, which is confidential: media-service refuses to
serve a location for one even after a clean virus scan, and the bucket policy
denies anonymous reads of the `documents/` prefix (§3c). The account number
used to actually move money belongs wherever payouts execute, encrypted at
rest — copying it here because onboarding collects it is how the most
sensitive data the platform holds ends up in the least protected place.

**Events.** Every transition emits exactly one outbox row, carrying the new
status and a denormalised `may_list_products`. That flag is denormalised on
purpose: a consumer deciding whether to accept a listing should not re-derive
the rule, because re-deriving is how two services end up disagreeing about who
may sell.

```
SellerRegistered  SellerDocumentsSubmitted  SellerReviewStarted
SellerApproved    SellerRejected            SellerActivated
SellerSuspended   SellerReinstated          SellerBanned
```

**Enforcement (implemented).** `catalog-service` refuses a product from a
seller who may not sell. It asks `GET /sellers/{id}/permission` — deliberately
narrow, so catalog receives neither a rejection reason nor an address — and
obeys the answer. It does not re-derive the rule from a status string, because
two services deriving "may this seller sell" is two services that will
eventually disagree, and the one that disagrees quietly keeps selling.

Synchronous, behind a Rule 11 circuit breaker, and **failing closed**. The
alternative was a local projection in catalog fed by the events above, which
wins on availability and loses on correctness — it is eventually consistent,
so a seller suspended for counterfeits keeps listing for as long as the lag
lasts. Creating a product is a cold path: no buyer request touches
seller-service, so refusing listings for the minutes it is down costs a retry,
while accepting them costs exactly the control.

Three refusals, three statuses, because they are three different instructions
to the caller — `403` will never succeed and must not be retried, `404` is a
bad id, `503` should be retried shortly. The check runs *before* the product
is built, so a refused listing leaves no row, no outbox message and nothing
for a read model to index.

The platform sentinel seller from migration 012 is now a real, active row in
`seller_db` (migration 015) rather than an exemption. A special-case id that
skips verification is the shape of thing that later gets reused for
"internal" listings and then for whatever else is inconvenient to onboard.

**Still not done, and named so it is not mistaken for done:** enforcement is
on *creation* only. A seller suspended after listing keeps their existing
products live — taking them down is a separate decision (§7 step 9 onward),
because it is a bulk state change with its own reversal, not a check.

`bff-seller` — the seller dashboard of orders, inventory, payouts and metrics
— is not built. Sellers must not reach internal services directly.

## 4. Webhook Deduplication Strategy (4-Layers)
External PSP webhooks must pass this exact sequence:
1. HMAC-SHA256 Signature Verification.
2. Redis SET NX (Fast path / 7-day TTL).
3. PostgreSQL INSERT ON CONFLICT DO NOTHING (Durable path).
4. Direct emission to the local outbox (No synchronous HTTP calls to internal services).

## 5. Event Mesh & Reliability Rules

### 5a. Redis availability (implemented)

Primary + replica + three sentinels, quorum two. Clients connect **through the
sentinels**, never to a hostname.

The thing being bought is failover time, not durability. Everything the
platform keeps in Redis has a durable counterpart — carts are in Postgres,
webhook dedup has `processed_webhooks` behind it, rate-limit budgets are
short-lived by design — and test_07 and test_08 prove losing the data is
survivable. What is not survivable is Redis being *unreachable*.

That is also why a bare replica would have been close to worthless: promoting
one by hand is the same outage with extra steps.

| Piece | Why it is that number |
|---|---|
| 3 sentinels | one can die and the rest still form a majority |
| quorum 2 | a quorum of 1 lets a sentinel that merely lost the network promote a replica while the real primary still takes writes |
| `master_for`, never `slave_for` | replication is async; a replica read can serve an emptied cart, or a rate-limit counter one increment behind — the second is a budget bypass |

**Sentinel must monitor an address, not a hostname, under Docker.** This cost a
full test run to find, and is the kind of failure the whole test tier exists
for: three healthy sentinels, a linked replica, and no promotion.

```
sentinel-1 | # Failed to resolve hostname 'redis'
sentinel-1 | # +tilt #tilt mode entered
```

With `resolve-hostnames yes`, sentinel re-resolves the monitored name while
checking on it. Docker's embedded DNS deletes a container's record the instant
the container dies — so the very event sentinel exists to react to is the event
that makes the name unresolvable. The failed lookup blocks its event loop long
enough to trip the TILT watchdog, and **a sentinel in TILT mode does not fail
over**. `docker/redis-sentinel.sh` resolves once at startup and monitors the
address.

**Node standardised on ioredis.** node-redis v4, which api-gateway used, has no
Sentinel support; websocket-gateway was already on ioredis. This removed a
Redis client rather than adding one. `rate-limit-redis` is client-agnostic —
it takes a `sendCommand` function.

Verified by `tests/e2e/test_10_redis_failover.py`, which kills the primary and
asserts the promotion happens (~6–9s observed), the cart survives, writes
resume, and the old primary rejoins as a replica rather than a second primary.
It discovers which container is primary rather than assuming, because sentinel
does not fail back.

### 5b. Broker replication (implemented)

Three brokers, `replication.factor=3`, `min.insync.replicas=2`, `acks=all`.
Each of those four is load-bearing and the set is not separable:

| Setting | Where | What its absence costs |
|---|---|---|
| 3 brokers | `docker-compose.yml` | RF=3 is unsatisfiable below three; a partition may not put two replicas on one broker |
| `default.replication.factor=3` | broker | topics created later silently return to one copy |
| `min.insync.replicas=2` | broker | `acks=all` degenerates to `acks=1` when followers fall behind |
| `acks=all` | every producer | a write acknowledged by a leader that dies before its followers copy it is gone |
| `unclean.leader.election.enable=false` | broker | a replica that never received the data can be elected leader, dropping acknowledged writes with no error anywhere |

Two brokers cannot express this: `min.insync.replicas=2` with RF=2 halts all
writes the moment either broker goes down, trading durability for an outage.
Three is the smallest count where one loss is survivable in both directions.

**Raising the broker count replicates nothing.** A topic keeps the replica
assignment it was created with, so every topic from the single-broker era stays
at one copy until it is reassigned — while the cluster reports green and every
produce succeeds. `scripts/kafka_replication.py` enumerates topics and reports
the ones below target; `--fix` reassigns them. This is not optional cleanup:
`__consumer_offsets` at RF=1 rewinds every consumer group to `earliest` on one
broker loss, and `_schemas` at RF=1 makes every Avro consumer unable to
deserialise anything.

One useful consequence of the combination: with `min.insync.replicas=2`, an
under-replicated topic **rejects writes** rather than accepting them
unreplicated. A producer using `acks=all` against an RF=1 topic gets
`NotEnoughReplicasException`. The failure is loud instead of silent, which is
the right way round.

`tests/e2e/test_09_broker_loss.py` stops a broker and asserts all of it:
acknowledged writes survive, writes continue during the outage, and the ISR
returns to three on its own afterwards.

Kafka data lives in named volumes (`kafka1_data`/`kafka2_data`/`kafka3_data`).
Before this there were none, so `compose down` discarded every event and every
consumer offset — the same defect the Redis volume fixed one layer down.

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

The first list was written for a single-tenant platform and is finished. What
follows is the marketplace order, and it is an order rather than a backlog:
each step is a precondition for the next, and skipping one produces work that
has to be redone rather than extended.

**Done, and provable from `git log` and `tests/e2e` rather than from this
list:**

1. Naming and responsibility conflicts resolved.
2. Env vars, ports, databases and compose infrastructure finalised.
3. Order lifecycle contract hardened; the saga closes and compensates.
4. Idempotency and outbox flow in every state-changing service.
5. Failure injection and recovery: broker loss, Redis failover, cache
   eviction, rate-limit state loss, concurrency under real contention.
6. Infrastructure durability: Kafka RF=3, Redis primary/replica/sentinel,
   object storage with a policy generated from the purpose taxonomy.
7. Sellers exist: onboarding, KYC references, versioned contracts (§3e).

**Next, in this order:**

8. ~~**Enforce seller permission on listing.**~~ ✅ done 2026-08-21.
   `catalog-service` refuses a product from a seller who may not sell,
   synchronously and failing closed (§3e). Enforcement is on creation only;
   taking an existing catalogue down when a seller is suspended is part of
   step 9's per-seller work.
9. ~~**Split orders per seller.**~~ ✅ done 2026-08-21. The `SellerOrder`
   aggregate in §3d, plus the order lines that had nowhere to live before it.
   Nothing advances a seller order past `PENDING` yet — that is step 10.
10. ~~**The COD order path.**~~ ✅ done 2026-08-21, except for refusal risk.
    The state vocabulary in §3d is implemented and driven, the reaper no
    longer releases stock under an order that is on a van, and delivery
    consumes stock where nothing consumed it before. Refusal-risk scoring in
    `fraud-service` is deliberately left: the signals are known, the model is
    not, and inventing one here would be a guess with a number attached.
11. ~~**Courier integration behind one contract**, in `fulfillment-service`,
    and the settlement reconciliation that follows from it.~~ ✅ done
    2026-08-21, as the contract. Provider mappings are config, and no real
    provider is configured yet — each needs a file written from that
    courier's own API documentation.
12. ~~**The escrow ledger**, in `payment-service`: money held between delivery
    and payout is a liability, recorded when it is collected.~~ ✅ done
    2026-08-21. Double entry, balanced on every write, booked at delivery.
    Executing a payout is not built — the ledger records one, the rails do
    not exist.
13. **`bff-seller`**, the seller dashboard. Sellers must never reach internal
    services directly (Rule 7).
14. **Ranking**: products and reviews first, then proximity — one pipeline,
    not per-surface sort orders.
15. **Media Center**, once the marketplace has sellers and orders (§3c).

Observability (§6) and security policy validation (Rule 8) are not steps in
this list. They are conditions on every step in it.

## 8. Truth Statement
This document is the current architectural source of truth for the repository. It is a living document and may be updated only when the architecture itself changes.
