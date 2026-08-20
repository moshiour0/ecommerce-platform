# Building the Marketplace

How to get from what exists today to a multi-vendor marketplace of the kind
Daraz and Alibaba run — without repeating the mistakes that make most attempts
collapse in year two.

This is an engineering plan, not a pitch. It names what to build, what to buy,
what to decide before writing code, and roughly what each phase costs.

---

## 0. Where you actually stand

Be clear-eyed about the starting point, because it changes what the plan should
be.

**What you have is real.** An event-driven core with a transactional outbox and
CDC, a saga with working compensation, pessimistic locking that provably does
not oversell, idempotency on every mutating path, 450 unit tests, 8 end-to-end
tests, four contention checks, and CI that runs on every push. Most teams
attempting this do not have any of it. The *shape* is right, and the shape is
the expensive part to change later.

**What you have is also about 10% of a marketplace, and single-tenant.** Every
model assumes one seller: products have no owner, inventory has no warehouse,
orders cannot be split, and there is no concept of a payout. Tax, promotions,
fraud and delivery quoting are 200-line placeholders. Nothing is deployed.

(Two items came off this list on 2026-08-16: multi-item checkout, where the
dispatcher reserved only the first line of every order, and `seller_id`, which
now exists on every product and flows through to search.)

So this plan is not "add features". Phases 0 and 1 are largely a **re-modelling**
of what exists, and that is normal: single-tenant to multi-tenant is the most
expensive migration in commerce software, and it only gets more expensive with
every row you add.

---

## 1. Five decisions to make before writing code

These are the choices that are cheap now and brutally expensive in eighteen
months. Everything else in this document is downstream of them.

### D1 — What a seller owns

**Decide:** whether a product belongs to a seller, or a seller offers a product.

Two models, and they are not interchangeable:

| Model | How it works | Who uses it |
|---|---|---|
| **Seller-owned listings** | Each seller creates their own product. Two sellers listing the same phone create two unrelated products. | Daraz, Alibaba, Etsy |
| **Shared catalog + offers** | One canonical product; sellers attach offers to it; a buy-box picks a winner. | Amazon |

**Recommendation: seller-owned listings, with an optional catalog spine later.**
It matches the marketplaces you named, it is far simpler, and it does not
require solving product matching — which is a machine-learning problem in its
own right and the reason Amazon's catalog team is enormous. Add a canonical
product layer later *only* if you find yourself needing a buy box.

**Cost of getting it wrong:** retrofitting a shared catalog onto seller-owned
listings means re-keying every product, every review, every order line, and
every search document.

### D2 — How an order splits, and how money moves

This is the single biggest structural change, and it is where most marketplace
codebases rot.

A customer places **one order** with one payment and one delivery address. But
fulfilment happens per seller: three sellers means three packages, three
shipping labels, three possible cancellations, three payouts, and three separate
refund paths.

**Model it as two levels from day one:**

```
Order                     customer-facing, one payment, one address
 └── SellerOrder          one per seller: its own status, shipment, payout
      └── OrderLine       product, quantity, price at time of sale
```

Your saga becomes **one saga per SellerOrder**, not per Order. The customer-level
Order is a thin aggregate that summarises its children.

**Money is the harder half.** In a marketplace you are holding other people's
money, which brings obligations:

- Funds sit in **escrow** until delivery is confirmed, then settle to the seller
  minus commission, minus refunds, minus penalties.
- A **ledger** — double-entry, append-only — is not optional. Every payout
  dispute is answered by it, and reconstructing balances from order rows is how
  marketplaces end up unable to explain what they owe.
- Payouts run on a cycle (weekly is typical), with holds for new sellers and for
  disputed orders.

**Cost of getting it wrong:** if you launch with a single-level order and add
splitting later, every order in history has to be migrated, and every report,
refund and payout written against the old shape has to be rewritten.

### D3 — Cash on delivery decides your architecture

If you are building for the Daraz markets — Bangladesh, Pakistan, Sri Lanka,
Nepal — **cash on delivery is the majority payment method, not an edge case.**
Assume 60–80% of orders. It changes everything downstream:

- There is no payment to capture at checkout, so your saga's `ChargePayment`
  step becomes conditional. The order is confirmed on a *promise*.
- **Return-to-origin (RTO)** becomes your dominant loss: customers refuse
  delivery, and you have paid shipping both ways for nothing. RTO rates of
  20–30% are normal, and managing them is a core business function, not an
  afterthought. You need buyer risk scoring, COD limits, address confidence,
  and phone verification before you need a recommendation engine.
- Money arrives from the **courier**, days later, in a lump sum that must be
  reconciled against individual orders.
- Fraud is different: the attack is not stolen cards, it is fake orders, address
  farming, and sellers gaming their own metrics.

**Decide this before you design the saga.** If COD is in scope, `fraud-service`
and the reconciliation ledger are Phase 1 concerns, not Phase 4 ones.

### D4 — One ranking pipeline, not three features

You described three discovery behaviours: personalised results, nearest-shop
preference, and location search. Built separately they will fight each other and
produce incoherent results.

**They are one pipeline with stages:**

```
query + user context
   ↓  intent        is this a product search, or a place name?
   ↓  retrieval     candidate set from Elasticsearch (text, filters, geo)
   ↓  scoring       relevance × quality × proximity × personalisation
   ↓  business      boosts, sponsored slots, diversity, availability
   ↓  results
```

Your stated rule — *quality first, then proximity* — is a scoring weight, not a
sort order. Concretely, in Elasticsearch:

```
final_score =  text_relevance
             × quality_boost      (rating, review count, sales velocity, low RTO)
             × distance_decay     (gauss decay, ~10km scale, floor 0.6)
             × personalisation    (affinity to category/brand/seller)
```

The distance term is a **decay, not a filter**: a shop 40 km away with excellent
reviews still outranks a mediocre shop next door, which is exactly the behaviour
you asked for. Never hard-filter by radius — that is the mistake that makes a
marketplace feel empty in low-density areas.

### D5 — The Media Center seam

You want to build it later but not paint yourself into a corner. Section 3.6
specifies exactly what to put in place now. The short version: **define the
contracts and the storage model now, build nothing else.**

---

## 2. Target architecture

### Your existing services

| Service | Verdict | What changes |
|---|---|---|
| `api-gateway` | **Keep** | Add seller and admin auth realms; per-user limits alongside per-IP |
| `bff-shop`, `bff-checkout` | **Keep** | Add `bff-seller` for the seller dashboard |
| `catalog-service` | **Re-model** | Products gain `seller_id`; variants; categories become a real taxonomy; approval workflow |
| `inventory-service` | **Re-model** | Stock per (seller, warehouse, sku), not per product |
| `order-saga` | **Re-model** | One saga per SellerOrder; conditional payment for COD |
| `payment-service` | **Replace** | Real PSP integration; never touch card data (see §4) |
| `cart-service` | **Keep** | Multi-seller carts; per-seller subtotals and shipping |
| `search-service` | **Rebuild** | The pipeline in D4; geo, facets, personalisation |
| `pricing-service` | **Keep** | Seller-set prices, campaign prices, price history |
| `promotion-service` | **Build out** | Vouchers, campaigns, seller-funded vs platform-funded |
| `tax-service` | **Build out** | Jurisdiction rules; VAT/GST; invoice generation |
| `fraud-service` | **Build out** | COD risk scoring is your highest-value model |
| `delivery-quote-service` | **Build out** | Real courier rates by weight, zone, service level |
| `fulfillment-service` | **Build out** | Courier integration, labels, tracking webhooks |
| `notification-service` | **Keep** | Add templates, localisation, quiet hours |
| `media-service` | **Keep** | Already has the quarantine lifecycle; add object storage |
| `audit-service` | **Keep** | Already tamper-evident; add seller and payout events |
| `user-service` | **Split** | Customer identity separate from seller identity (D3) |

### New services

| Service | Responsibility |
|---|---|
| `seller-service` | ✅ **Built** — onboarding state machine, KYC document references, versioned contracts, shop profile, seller status |
| `settlement-service` | The double-entry ledger, commission, payouts, reconciliation |
| `review-service` | Product and seller reviews, ratings, moderation, verified-purchase proof |
| `geo-service` | Places index, geocoding, address normalisation, serviceability |
| `personalisation-service` | Behaviour events → features → recommendations |
| `returns-service` | RMA workflow, refund authorisation, RTO handling |
| `chat-service` | Buyer–seller messaging (table stakes on Daraz and Alibaba) |

That is roughly **27 services**. Resist adding more: every service is an
on-call surface, a deployment, and a schema to migrate.

### Rules that still hold

Everything in `ARCHITECTURE_STATE_FINAL.md` continues to apply, and two rules
matter more in a marketplace than they did before:

- **Rule 1, data isolation.** With sellers in the picture, a cross-database join
  becomes a data-leak vector, not just a coupling problem.
- **Rule 4, idempotency.** COD reconciliation replays courier files. Without
  idempotency you will double-credit sellers.

---

## 3. The features you named, designed

### 3.1 Seller onboarding

The flow, and each step is a state a seller can be stuck in — model it as an
explicit state machine, not booleans:

```
registered → documents submitted → under review → approved → active
                                       ↓
                                    rejected / suspended
```

- **KYC**: national ID, trade licence, bank account, tax registration. Store
  documents in object storage, never in Postgres, and treat them as
  confidential — this is the most sensitive data you will hold.
- **Contracts**: commission rates by category, versioned, with the accepted
  version recorded against the seller.
- **Product approval**: new sellers' listings go to a moderation queue.
  Established sellers publish immediately. This is the single most effective
  control against counterfeit and prohibited goods.
- **Seller dashboard**: orders, inventory, payouts, performance metrics. Build
  it as `bff-seller`; do not let sellers hit internal services directly.

**Seller performance metrics drive your ranking** (D4), so define them early:
on-time dispatch, cancellation rate, return rate, response time, rating.

### 3.2 Order splitting and fulfilment

```
Customer checks out
  → Order created (one payment intent, one address)
  → split into SellerOrders by seller_id
  → per SellerOrder: reserve stock → confirm → seller dispatches
                     → courier picks up → in transit → delivered
  → on delivery: capture payment (card) or record collection (COD)
  → settle to seller after the return window closes
```

Each SellerOrder runs your existing saga shape. The important discipline: **a
customer must never see one seller's failure as a whole-order failure.** If one
seller is out of stock, that SellerOrder cancels and refunds; the rest ship.

### 3.3 Search and discovery

Elasticsearch, one index per concern:

- `products` — the searchable listing, denormalised with seller and shop fields
- `shops` — for location search, with a `geo_point`
- `places` — administrative areas and neighbourhoods, for intent detection

Query-time facets: category, price range, brand, rating, shipping speed, seller
tier, and location. Precompute nothing that changes hourly; Elasticsearch
aggregations are fast enough.

**Personalisation** enters as a scoring signal, not a separate system: the user's
category and brand affinities are a small vector fetched from
`personalisation-service` and applied as a boost. If that service is down,
search degrades to non-personalised — never to broken.

### 3.4 Location search

When someone types a place name, the intent is different and the result set is
different.

1. **Intent detection.** Match the query against the `places` index first. High
   confidence and no strong product match means place intent.
2. **Disambiguate visibly.** Show a chip: *Showing shops in Gulshan · Search
   products instead*. Never silently switch modes — a search box that guesses
   wrong and says nothing is infuriating.
3. **Resolve to geometry.** A place resolves to a polygon or a centre-plus-radius,
   and shops are filtered by it.
4. **Rank within it** by the same quality-first formula.

Build the `places` index from OpenStreetMap administrative boundaries. Do not
attempt to build a geocoder.

### 3.5 Personalisation

Start far simpler than you think, because a cold-start recommender that returns
nothing is worse than a best-seller list.

**Stage 1 — behaviour capture.** Every view, search, add-to-cart and purchase
becomes an event on Kafka. You already have the pipeline for this; it is the
same outbox and CDC path. Consent-gate it, and keep raw behaviour separate from
identity so it can be deleted on request.

**Stage 2 — cheap and effective.** "Customers who bought this also bought"
(co-visitation counts), recently viewed, trending in your area, and
category affinity. This gets you most of the value of personalisation for a
fraction of the cost, and it needs no ML infrastructure.

**Stage 3 — learned ranking.** Only once you have months of behaviour data and
someone who can own a model, add learning-to-rank over the signals you already
compute.

**Do not** start at stage 3. Marketplaces with far larger teams than yours run
stage 2 for years.

### 3.6 Media Center — the seam to build now

The goal is that Media Center can be built later **without touching commerce**.
That requires four things now, and nothing else.

**1. Object storage from day one.** ✅ **Done.** `media-service` already owned
metadata and lifecycle — that was the right seam. MinIO now runs in compose,
`POST /media/{id}/upload-url` issues a pre-signed PUT so bytes never pass
through a service, and the bucket policy is generated from the purpose taxonomy
rather than written by hand. Every image the marketplace already needs —
product photos, KYC documents — goes through that same path, which is why this
was not speculative work. Swapping MinIO for S3 is three environment
variables.

**2. An asset purpose.** Add `purpose` to media assets now:

```
product_image | seller_document | try_on_source | post_media
```

One column, no logic. It means try-on sources and social posts have a home the
day someone starts building them, and the quarantine and scanning you already
built applies to user-uploaded body photos automatically — which is exactly the
content you most need scanned.

**3. Event contracts, defined and unused.** Declare these now in the
architecture document, emit nothing:

```
MediaUploaded        (exists as MediaRegistered)
MediaPublished       (exists)
TryOnRequested       asset_id, product_id, user_id
TryOnCompleted       source_asset_id, result_asset_id, product_id, fit_metadata
PostCreated          post_id, user_id, media_ids[], product_ids[]
PostEngaged          post_id, user_id, kind (like|comment|share)
```

Reserve topic names and the port range 8020–8029 for Media Center services. A
contract costs a paragraph today and saves a migration later.

**4. A hard boundary.** Media Center is a **separate bounded context with its own
database**, and commerce may never call it synchronously. A product page that
cannot render because the social feed is slow is a self-inflicted outage. The
integration is: commerce emits events, Media Center consumes them; Media Center
emits events, commerce may consume them. Nothing blocks.

When you do build it: try-on is an **asynchronous job pipeline** — upload, queue,
GPU worker, result stored as a new media asset, user notified. Treat it exactly
like a print job. The social feed is a fan-out-on-read timeline; do not build
fan-out-on-write until you have a reason.

---

## 4. Where world-class teams do not build

This is the part that separates teams that ship from teams that spend two years
on infrastructure. **Build your differentiator; buy everything else.**

| Concern | Buy | Why not build |
|---|---|---|
| Card payments | Stripe, Adyen, or local (SSLCommerz, bKash, Nagad) | PCI-DSS scope. Never let card data touch your servers — use hosted fields and stay in SAQ-A |
| Object storage + CDN | S3 + CloudFront, or equivalent | Storage is not your product |
| Image processing | imgproxy, Cloudinary, or Thumbor | Resizing at the edge is a solved problem |
| Email / SMS / push | SES, Twilio, FCM | Deliverability is a full-time speciality |
| Geocoding, maps | OpenStreetMap / Nominatim, Google Maps | Do not build a geocoder |
| Search infrastructure | Managed Elasticsearch or OpenSearch | Run the queries, not the cluster |
| Kafka | MSK, Confluent Cloud, or Redpanda | Operating Kafka is a job |
| Postgres | Managed, with read replicas and PITR | Backups you have never restored are not backups |
| Observability | Grafana Cloud, Datadog | Wiring dashboards is not differentiating |
| Courier logistics | Pathao, Steadfast, RedX, or aggregators | Their network is the product |

**What you must build:** the marketplace domain itself, the seller experience,
the discovery pipeline, and the Media Center. That is where your product is
different. Everything above is where it is identical to everyone else's.

---

## 5. What "enterprise level" actually means

Not features — properties. Five of them, and each has a number attached.

### Availability

Define SLOs before you need them. A reasonable starting set:

| Journey | Target | Meaning |
|---|---|---|
| Browse and search | 99.9% | ~43 min/month of error budget |
| Checkout | 99.95% | ~22 min/month |
| Payment capture | 99.99% | ~4 min/month |
| Seller dashboard | 99.5% | Internal-facing, can degrade |

Then engineer to them: multi-AZ from day one, multi-region only when a number
justifies it. Every dependency gets a circuit breaker (your Rule 11) and a
defined degraded mode — search without personalisation, product page without
reviews, checkout without delivery estimates.

### Scale

Size for your actual traffic and one order of magnitude above, not for Amazon:

- Postgres: managed, read replicas for reporting, **partition** orders and events
  by month before the tables reach ~50M rows. Shard only when a single primary
  genuinely cannot cope — it is a large step and most marketplaces never take it.
- Kafka: replication factor **3**, minimum in-sync replicas 2. ✅ **Done** —
  compose runs three brokers with `acks=all` and unclean leader election off,
  and `tests/e2e/test_09_broker_loss.py` proves a broker can die without losing
  an acknowledged write. In production this becomes a managed cluster across
  three availability zones; the settings are the same.
- Redis: cluster or sentinel. ✅ **Done** — primary, replica and three sentinels
  with quorum two, and every client connects through the sentinels rather than
  to a hostname. `tests/e2e/test_10_redis_failover.py` kills the primary and
  proves the promotion happens, carts keep serving and writes resume. In
  production this becomes a managed Redis with automatic failover across
  availability zones; the client configuration is the same.
- Elasticsearch: 3 data nodes minimum; index per month for behaviour data.

Load-test before every peak sale, and know your numbers: orders per second at
peak, cart writes per second, search queries per second.

### Security

- **mTLS between services** (Istio or Linkerd). Your Rule 8 mandates it and it is
  unimplemented — a single compromised service currently reaches every database.
- **Secrets in Vault or a cloud secret manager**, not environment variables.
- **PCI scope minimisation**: card data never enters your infrastructure.
- **Seller data isolation**: a seller must never be able to enumerate another
  seller's orders. Enforce it in the query layer and test it explicitly — it is
  the most common serious bug in marketplace software.
- **Abuse**: per-user rate limits, bot detection on search and checkout, and
  review-fraud detection. Your gateway limits by IP only, which one mobile
  carrier NAT defeats.

### Compliance

- Data rights: export and deletion, with behaviour data deletable independently
  of order history (which you must retain for tax).
- Consent for tracking, recorded and revocable — personalisation depends on it.
- Tax invoices per jurisdiction, immutable and sequentially numbered.
- Seller KYC retention rules.

### Operations

- **CD with canary or blue-green**, feature flags, and a rollback you have
  practised. You have CI; you have no CD.
- On-call rotation, runbooks, and alerting on **SLO burn rate**, not CPU.
- Backup restores tested quarterly. An untested backup is a hope.
- Chaos testing on the payment and inventory paths once the basics hold.

---

## 6. Roadmap

Sizing assumes a team of **6–10 engineers**. With 2–3, multiply by three and cut
scope hard. These are not padded; marketplaces take this long.

### Phase 0 — Make the foundation honest (1–2 months)

Fix what is already broken before building on it.

- Real authentication: registration, login, sessions, password reset
- Replace the simulated payment service with a real PSP in sandbox
- mTLS and a secret manager
- Deploy to a real cluster; get CD and one-command rollback working

**Exit:** a single-seller order can be placed, paid, shipped and refunded in a
deployed environment, with tests proving it.

### Phase 1 — Multi-vendor core (3–4 months)

The re-modelling. The expensive phase, and the one that must not be rushed.

- `seller-service`, onboarding and KYC
- Products, inventory and pricing keyed by seller
- Order splitting into SellerOrders; saga per SellerOrder
- `settlement-service`: ledger, commission, payouts
- COD flow end to end, including courier reconciliation (if in scope — D3)
- Seller dashboard (`bff-seller`)

**Exit:** three sellers can onboard, list, sell, ship and be paid, with the
ledger balancing to the cent.

### Phase 2 — Discovery (2–3 months)

- Search pipeline rebuild: relevance, facets, quality scoring
- Geo: shop locations, distance decay, serviceability
- `places` index and location-name search
- `review-service` with verified-purchase reviews

**Exit:** the ranking behaves as specified — a well-reviewed distant shop beats
a poor nearby one — and it is measured, not asserted.

### Phase 3 — Logistics and trust (2–3 months)

- Courier integrations, labels, tracking webhooks
- `returns-service`: RMA, refunds, RTO handling
- `fraud-service`: COD risk scoring, address confidence, buyer/seller limits
- `chat-service`: buyer–seller messaging

**Exit:** returns and RTO are handled without manual intervention for the common
cases.

### Phase 4 — Personalisation (2 months)

- Behaviour event pipeline with consent
- Stage-2 recommendations (co-visitation, recently viewed, local trends)
- Personalisation as a search boost
- A/B testing infrastructure — without it you cannot tell if any of this works

**Exit:** a measurable lift in conversion, proven by experiment.

### Phase 5 — Media Center (4–6 months, and its own team)

Only once the marketplace is working and has users worth engaging.

- Object storage and upload pipeline (already seamed in Phase 0)
- Try-on: pose estimation, garment fitting, 3D preview, GPU job pipeline
- Social feed: posts, follows, timeline, moderation
- Commerce integration by events only

**Total: roughly 14–20 months to a credible marketplace**, with Media Center
after. Anyone promising less is selling something.

---

## 7. The next two weeks

Concrete, small, and each one buys information or removes risk:

1. **Decide D1, D2 and D3 in writing** and add them to
   `ARCHITECTURE_STATE_FINAL.md`. Half a day, and it determines a year of work.
2. **Add the Media Center seam** from §3.6 — the `purpose` column, the event
   contracts in the architecture document, the reserved ports. One afternoon.
3. ~~**Set Kafka to RF=3 and give Redis a replica**~~ ✅ done, both. The local
   environment now fails over rather than teaching habits that break in
   production.

`seller_id` landed on 2026-08-16: every product has an owner, the event and
the read model carry it, and search can be filtered to one seller. The Media
Center seam, object storage and Kafka RF=3 landed on 2026-08-17, and Redis
failover on 2026-08-21. That closes the near-term infrastructure list.

D1-D3 are answered (§8). Seller onboarding landed on 2026-08-21:
`seller-service` on port 8020 with the state machine from §3.1, KYC documents
held as references to confidential media assets, and versioned contracts.

Two things it deliberately does not do yet, both named in
ARCHITECTURE_STATE_FINAL.md §4b: catalog-service does not yet refuse listings
from sellers who may not sell, and there is no `bff-seller`. The COD order
path is the other half of the near-term product work.

---

## 8. The three decisions, answered

Answered 2026-08-21. These are no longer open, and the rest of this document
should be read through them.

**D1 - Market: Bangladesh.** Daraz is the reference, not Alibaba's B2B model.
Consumer marketplace, many sellers, one cart.

The consequence is larger than a payment method: **cash on delivery is the
primary path, and card is secondary.** COD is not a payment option bolted onto
a card flow — it inverts the order lifecycle:

| | Card-first | COD (what we are building) |
|---|---|---|
| When money moves | at checkout, before fulfilment | at delivery, days later |
| What the saga waits on | a PSP webhook | a courier settlement file |
| The loss vector | chargebacks | **return-to-origin** — refused deliveries, shipping paid twice |
| What fraud scores | stolen cards | likelihood of refusal at the door |
| Inventory hold | minutes | days, until delivery confirms |
| Seller payout | PSP payout schedule | courier remittance, reconciled |

Build the COD path first and the card path second. A design that assumes
card-first needs rework through the saga, fraud, ledger and reservation TTLs
simultaneously.

**D2 - Team: 10 engineers.** The phased plan above assumed 6-10, so the
phasing stands as written. Ten is enough to run three or four streams in
parallel — marketplace core, payments/COD, search and ranking, seller tools —
but it is not enough to also build the Media Center's ML. That stays a seam
(§3.6) until the marketplace earns it.

**D3 - Differentiator: the Media Center.** Try-on, 3D view and the social feed
are the actual bet, not decoration. That justifies the seam work already done —
the `purpose` taxonomy, the reserved ports, the declared event contracts, the
object-storage prefixes with their own access policy — and it means the
commerce side must never call it synchronously. A product page that cannot
render because a feed is slow is a self-inflicted outage.

It does **not** justify building try-on before the marketplace works. The
counter-argument in the original version of this section — put a thin
marketplace on someone else's platform and spend the engineering on try-on —
is worth restating once and then setting aside: with ten engineers and a
COD-first Bangladeshi market, the marketplace *is* the hard part, and a
try-on feature attached to a marketplace that cannot pay its sellers is a demo.

### What this fixes about the plan

- COD moves into Phase 1, not Phase 2. The order saga, the escrow ledger and
  the courier integration are the same piece of work and cannot be sequenced
  apart.
- RTO prediction becomes a first-class job for `fraud-service`, replacing the
  card-fraud framing it currently has.
- Local courier integrations (Pathao, Steadfast, RedX, Sundarban and the rest)
  are Phase 1 infrastructure, not an afterthought. Each is an external system
  with its own settlement format; they belong behind one internal contract from
  the start, exactly as the PSP is.
- Bangla language support and BDT-only pricing simplify the money model — Rule
  6's integer cents becomes integer poisha — but multi-currency should stay
  *possible*, since cross-border sourcing is the obvious later expansion.
- The Media Center's ML work stays out of the critical path until the
  marketplace has sellers and orders.
