# ANTI-GRAVITY MASTER ROADMAP & CHECKPOINT STATE

> ## SUPERSEDED — 2026-08-21
>
> This file plans a **single-tenant** e-commerce platform. The product is now a
> **multi-vendor marketplace for Bangladesh with cash on delivery as the
> primary payment path**, which changes the order lifecycle rather than adding
> to it.
>
> It is kept for history and should not be used to decide what to build.
>
> | For | Read |
> |---|---|
> | What to build next, in order | [ARCHITECTURE_STATE_FINAL.md](ARCHITECTURE_STATE_FINAL.md) §7 |
> | The architecture and its rules | [ARCHITECTURE_STATE_FINAL.md](ARCHITECTURE_STATE_FINAL.md) |
> | Why the marketplace is shaped this way | [MARKETPLACE_ROADMAP.md](MARKETPLACE_ROADMAP.md) |
> | What is actually built | `git log`, and `tests/e2e` |
>
> Three documents disagreeing about status is the exact failure the note below
> was written about. Rather than let this one drift into being a fourth, it is
> marked closed.


> **What `[X]` means here: scaffolded and designed — not verified working.**
>
> This file previously declared `CURRENT STATUS: [PHASE 1 - Step 1.1]` in its
> header while every phase below was ticked `[X]`. Read as a status report it
> said the platform was finished; it is not. Roughly half the service
> directories contain substantive logic, every Kubernetes manifest is empty,
> and the saga could not advance outside a test run.
>
> A phase is only complete when a test proves it. Until a phase has a passing
> test, `[X]` here means "the files exist." Implementation truth lives in
> `git log` and in the code.

## PHASE 1: Base Infrastructure Verification (The Concrete)
- [X] 1.1: Verify Docker-Compose spin-up (Postgres, Redis, Kafka, Zookeeper, Elastic).
- [X] 1.2: Validate isolated database creation via `init-dbs.sh`.

## PHASE 2: Core Domain Services (No Dependencies)
*These services own their data and rely on nothing else.*
- [X] 2.1: `catalog-service` (Postgres) - Product models, variants, categories.
- [X] 2.2: `user-service` (Postgres) - Identity, profiles, address books.

## PHASE 3: Stateful & Rule Services (Tier 1 Dependencies)
*These services rely on Catalog and User data to function.*
- [X] 3.1: `pricing-service` (Postgres) - Base price calculations.
- [X] 3.2: `promotion-service` (Postgres) - Discount logic.
- [X] 3.3: `tax-service` (Postgres) - Tax rules.
- [X] 3.4: `cart-service` (Redis) - Ephemeral state, TTL locks, connects to Catalog/Pricing.

## PHASE 4: The Event Mesh & Outbox Pattern (The Nervous System)
*Implementing the Kafka/Debezium pipeline before we handle orders.*
- [X] 4.1: Shared Python Library `outbox.py` & `kafka_client.py` implementation.
- [X] 4.2: CDC Connector deployment and validation in Debezium.

## PHASE 5: The Checkout Saga (The Brain)
*The most complex orchestration in the system.*
- [X] 5.1: `inventory-service` (Postgres) - Pessimistic row-level locking (SELECT FOR UPDATE).
- [X] 5.2: `fraud-service` (Postgres) - Velocity limits, zero-click/malicious APK telemetry validation.
- [X] 5.3: `payment-service` (Postgres) - PCI tokenization, intent creation.
- [X] 5.4: `order-saga` (Postgres) - The State Machine (Pending -> Reserved -> Paid -> Confirmed). Distributed locking.

## PHASE 6: Post-Order & Delivery 
- [X] 6.1: `delivery-quote-service` (Postgres) - Pre-checkout courier API proxy.
- [X] 6.2: `fulfillment-service` (Postgres) - Post-checkout label generation and dispatch.
- [X] 6.3: `notification-service` (Postgres) - Email/SMS/Push dispatch.

## PHASE 7: Read Models & Search
- [X] 7.1: `stream-processor` (Worker) - Consumes Kafka events, denormalizes data.
- [X] 7.2: `search-service` (Elasticsearch) - Fast, read-only querying for the frontend.

## PHASE 8: Edge, BFFs, and Security (The Shield)
- [X] 8.1: `bff-shop` (Node.js) - Orchestrating catalog, search, and cart.
- [X] 8.2: `bff-checkout` (Node.js) - Orchestrating the saga, validating final state.
- [X] 8.3: `api-gateway` (Node.js) - JWT validation, rate limiting, OpenAPI schema validation.
- [X] 8.4: `websocket-gateway` (Node.js) - Real-time client updates.
