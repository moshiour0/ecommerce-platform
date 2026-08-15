---
config:
  layout: elk
---
flowchart TD
%% =====================================================================
%% REVISION NOTES — changes from the previous revision are tagged [FIX-n]
%% [FIX-1] Ingress request path drawn (Client -> WAF -> Ingress -> BFF).
%%         Previously WAF and Istio had zero edges and the primary
%%         request path of the platform was absent, contradicting Rule 6.3.
%% [FIX-2] Saga return path drawn. Previously all 7 saga arrows pointed
%%         outward and nothing came back, so no component could advance
%%         the state machine. Commands and events now round-trip through
%%         the outbox/CDC/Kafka mesh via SagaDispatcher, satisfying Rule 3.
%% [FIX-3] Payment compensation added (RefundPaymentCommand /
%%         PaymentRefunded). Previously "confirm / compensate" pointed at
%%         inventory only: money could be taken with no path to return it.
%% [FIX-4] BFF vs Saga ownership resolved. BFF performs read-only
%%         pre-flight quotes; only the saga mutates money or stock.
%%         bff-checkout no longer calls PaymentSvc directly.
%% [FIX-5] bff-checkout -> CatalogSvc / PricingSvc drawn, as Rule 7
%%         already required but the diagram omitted.
%% [FIX-6] SearchDB removed. search-service owns Elasticsearch only
%%         (Rule 1: one datastore per service).
%% [FIX-7] Orphaned workers given owners and drawn into their flows.
%% [FIX-8] media-service placed inside its own pipeline; the storage
%%         nodes are its infrastructure, not free-floating components.
%% [FIX-9] Notification ownership split: service owns state + API,
%%         worker owns provider dispatch.
%% =====================================================================

%% =========================
%% 1. EDGE & EXPERIENCE
%% =========================
subgraph Edge["1. Edge & Experience Layer"]
  Client[Web / Mobile Apps]
  Admin[Admin / Ops Portal]
  WAF["WAF / CDN<br/>(DDoS, Bot Control, injects trace_id)"]
  Ingress["Ingress Gateway :8000<br/>(JWT, Tiered Rate Limit, OpenAPI Validation)"]
  BFFShop["Shop BFF :8001<br/>(Read-Heavy, Circuit Breaker)"]
  BFFCheckout["Checkout BFF :8002<br/>(Pre-flight quotes only, Circuit Breaker)"]
  WSGW["WebSocket Gateway :8003<br/>(Redis Pub/Sub, Reconnect)"]
end

%% =========================
%% 2. PLATFORM FOUNDATIONS
%% =========================
subgraph Platform["2. Platform Foundations (Zero Trust)"]
  Mesh["Istio Control Plane<br/>(mTLS, SPIFFE, AuthZ)"]
  IAM["Identity Provider<br/>(OIDC / SSO)"]
  Vault["Vault<br/>(Secrets, KMS, Tokenization)"]
  OTel["OpenTelemetry Plane<br/>(Traces, Metrics, Logs)"]
  Config[Config & Feature Flags]
  Autoscale["Kubernetes HPA / KEDA<br/>(Autoscaling)"]
  Backup[Backup / Restore / DR]
end

%% =========================
%% 3. CORE SERVICES & DATA
%% =========================
subgraph Core["3. Core Services & Datastores"]
  Redis[(Redis<br/>Cart State, Rate Limits, WS Pub/Sub)]

  CatalogSvc[Catalog Svc :8005]
  CatalogDB[(Catalog DB)]

  SearchSvc[Search Query Svc :8006]

  CartSvc["Cart Svc :8007<br/>(Checkout Mutex)"]
  CartDB[(Cart DB)]

  PricingSvc[Pricing Svc :8008]
  PromoSvc[Promotion Svc :8009]
  TaxSvc[Tax Svc :8010]
  DeliveryQuoteSvc[Delivery Quote Svc :8011]

  UserSvc[User / Profile Svc :8004]
  UserDB[(User DB)]

  OrderSaga["Order Saga Orchestrator :8012<br/>(State Machine, authoritative)"]
  SagaReaper["Saga Reaper<br/>(in-process, 60s sweep, 15min timeout)"]
  OrderDB[(Order DB)]

  InventorySvc[Inventory Svc :8013]
  InventoryDB[(Inventory DB)]

  FraudSvc["Fraud / Risk Svc :8014<br/>(Velocity: User+IP+DeviceFP)"]

  PaymentSvc["Payment Svc :8015<br/>(PCI Tokenized, charge + refund)"]
  PaymentLedger[(Payment Ledger DB)]

  NotificationSvc["Notification Svc :8016<br/>(owns state + API)"]
  NotificationDB[(Notification DB)]

  FulfillmentSvc[Fulfillment Svc :8017]
  FulfillmentDB[(Fulfillment DB)]

  MediaSvc["Media Svc :8018<br/>(owns upload lifecycle)"]
  AuditSvc["Audit Svc :8019<br/>(immutable records)"]
  AuditDB[(Audit DB)]
end

%% =========================
%% 4. EVENT MESH
%% =========================
subgraph Events["4. Event Mesh (Strict Correctness)"]
  CDC["Outbox / CDC<br/>(Debezium)"]
  Kafka[[Kafka Cluster]]
  Registry["Schema Registry<br/>(Avro / Protobuf, FULL_TRANSITIVE)"]
  DLQ[["Dead Letter Queues<br/>(dlq.topic, alert < 5min)"]]
  DLQReprocessor["DLQ Reprocessor :8032<br/>(Backoff Retry x3, then Escalate)"]
end

%% =========================
%% 4b. SAGA DISPATCH  [FIX-2]
%% The component that closes the saga loop. Previously undrawn, which is
%% why no production implementation existed. Consumes commands, invokes
%% the owning service, then consumes that service's result event and
%% advances the state machine. It holds no business logic.
%% =========================
subgraph Dispatch["4b. Saga Command Dispatch"]
  SagaDispatcher["Saga Dispatcher :8030<br/>(Command consumer + Event feeder)"]
end

%% =========================
%% 5. SEARCH INDEXING
%% =========================
subgraph SearchPipe["5. Search Indexing & Read Models"]
  Stream["Stream Processor :8031<br/>(Kafka consumer, denormalizer)"]
  Reindex[Backfill / Reindex Job :8033]
  Elastic[(Elasticsearch / OpenSearch)]
end

%% =========================
%% 6. SECURE MEDIA
%% =========================
subgraph Media["6. Secure Media Pipeline"]
  Upload[Pre-Signed Upload]
  Quarantine[("Quarantine Storage<br/>(TTL)")]
  Scanner[Malware / Size / Policy Scanner]
  Clean[(Clean Object Storage)]
  MediaMeta[(Media Metadata DB)]
  CDN[(Public CDN)]
end

%% =========================
%% 7. NOTIFICATIONS & EXTERNAL INTEGRATIONS
%% =========================
subgraph Notify["7. Notifications & External Integrations"]
  NotifyWorker["Notification Worker :8034<br/>(owns provider dispatch)"]
  Email[Email Provider]
  SMS[SMS Provider]
  Push[Push Provider]
  PSP[External Payment Gateway / PSP]
  Webhooks["Webhook Handler :8035<br/>(4-Layer Dedup)"]
  Courier[Shipping / Courier API]
end

%% =========================
%% 8. OPERATIONS & RELIABILITY
%% =========================
subgraph Ops["8. Operations & Reliability"]
  Dash[Dashboards / SLOs]
  Logs[Centralized Logs]
  Trace[Distributed Traces]
  Alerting[Alertmanager / On-call]
  CI[CI/CD Pipeline]
  Canary[Blue-Green / Canary Deploy]
end

%% =====================================================================
%% INGRESS REQUEST PATH  [FIX-1]
%% Matches the Rule 6.3 trace path: Client -> WAF -> Gateway -> BFF -> Svc
%% =====================================================================
Client --> WAF
Admin --> WAF
WAF --> Ingress
Ingress --> BFFShop
Ingress --> BFFCheckout
Client -. Auto-reconnect .-> WSGW

Ingress --> IAM
Config -. config .-> Ingress
Autoscale -. scales .-> Ingress
Mesh -. mTLS / AuthZ .-> Core
Mesh -. mTLS / AuthZ .-> Dispatch
Vault -. secrets .-> Ingress
Vault -. secrets .-> Core

%% =====================================================================
%% BFF FAN-OUT  [FIX-4] [FIX-5]
%% BFFs are read-only orchestrators. They may quote, validate and
%% pre-screen. They may NOT reserve stock or move money — those are
%% saga-owned transitions.
%% =====================================================================
BFFShop -->|Circuit Breaker| CatalogSvc
BFFShop -->|Circuit Breaker| SearchSvc
BFFShop -->|Circuit Breaker| CartSvc
BFFShop -->|Circuit Breaker| PricingSvc
BFFShop -->|Circuit Breaker| PromoSvc
BFFShop -->|Circuit Breaker| UserSvc

BFFCheckout -->|Circuit Breaker| CartSvc
BFFCheckout -->|"Rule 7: validate (sync)"| CatalogSvc
BFFCheckout -->|"Rule 7: validate (sync)"| PricingSvc
BFFCheckout -->|"pre-flight quote"| TaxSvc
BFFCheckout -->|"pre-flight quote"| DeliveryQuoteSvc
BFFCheckout -->|"pre-screen"| FraudSvc
BFFCheckout -->|"submit order"| OrderSaga

%% =========================
%% SERVICE -> DATASTORE  [FIX-6] [FIX-8]
%% =========================
CartSvc --> Redis
WSGW --> Redis
Ingress --> Redis

CatalogSvc --> CatalogDB
CartSvc --> CartDB
UserSvc --> UserDB
OrderSaga --> OrderDB
SagaReaper --> OrderDB
InventorySvc --> InventoryDB
PaymentSvc --> PaymentLedger
NotificationSvc --> NotificationDB
FulfillmentSvc --> FulfillmentDB
MediaSvc --> MediaMeta
AuditSvc --> AuditDB
SearchSvc --> Elastic

%% =====================================================================
%% SAGA COMMAND / EVENT LOOP  [FIX-2] [FIX-3]
%% Outbound: saga writes a command to its outbox in the same transaction
%% as the state change (Rule 3). CDC publishes it. The dispatcher invokes
%% the owning service.
%% Inbound: the service writes its result to its OWN outbox (carrying
%% order_id for correlation). CDC publishes it. The dispatcher feeds it
%% back as a state transition. Nothing bypasses the outbox.
%% =====================================================================
Kafka -->|OrderSaga.commands| SagaDispatcher

SagaDispatcher -->|1. ReserveInventoryCommand| InventorySvc
SagaDispatcher -->|2. ChargePaymentCommand| PaymentSvc
SagaDispatcher -->|3. ConfirmOrderCommand| FulfillmentSvc
SagaDispatcher -->|"C1. ReleaseInventoryCommand (compensate)"| InventorySvc
SagaDispatcher -->|"C2. RefundPaymentCommand (compensate)"| PaymentSvc

Kafka -->|"Inventory.events / Payment.events / Fulfillment.events"| SagaDispatcher
SagaDispatcher -->|"POST /orders/{id}/events"| OrderSaga

SagaReaper -.->|"sweeps PENDING / INVENTORY_RESERVED / PAID at 15min"| OrderDB
SagaReaper -.->|"emits ReleaseInventory + RefundPayment via outbox"| OrderDB

%% Pre-flight risk is BFF-owned; the saga does not re-check.
%% Fraud writes its verdict to its outbox for audit only.
FraudSvc --> Kafka

%% =========================
%% OUTBOX -> CDC (Rule 3)
%% =========================
CatalogDB --> CDC
CartDB --> CDC
OrderDB --> CDC
InventoryDB --> CDC
PaymentLedger --> CDC
UserDB --> CDC
NotificationDB --> CDC
FulfillmentDB --> CDC
MediaMeta --> CDC

CDC --> Kafka
Kafka <--> Registry
Kafka -. Poison pills .-> DLQ
DLQ --> DLQReprocessor
DLQReprocessor -.->|Retry up to 3x| Kafka
DLQReprocessor -.->|Escalate after 3x| AuditSvc
DLQ -.->|Alert within 5min| Alerting

%% =========================
%% READ MODELS
%% =========================
Kafka --> Stream
Stream --> Elastic
Reindex --> Elastic
CDC --> Reindex

%% =========================
%% MEDIA PIPELINE  [FIX-8]
%% =========================
Client -->|pre-signed URL request| MediaSvc
MediaSvc --> Upload
Upload --> Quarantine
Quarantine --> Scanner
Scanner -->|clean| Clean
Scanner -->|"infected: quarantine + alert"| Alerting
Clean --> CDN
Scanner --> MediaSvc

%% =========================
%% NOTIFICATIONS  [FIX-9]
%% =========================
Kafka --> NotifyWorker
NotifyWorker -->|"record dispatch state"| NotificationSvc
NotifyWorker --> Email
NotifyWorker --> SMS
NotifyWorker --> Push

%% =========================
%% EXTERNAL INTEGRATIONS  [FIX-3]
%% =========================
PaymentSvc -->|charge / refund| PSP
PSP -->|async settlement webhook| Webhooks
Webhooks -->|"4-layer dedup, then local outbox"| PaymentLedger

DeliveryQuoteSvc --> Courier
FulfillmentSvc --> Courier

%% =========================
%% AUDIT & OBSERVABILITY
%% =========================
Kafka -->|"all business events"| AuditSvc
Vault --> AuditSvc

Backup -. snapshot .-> OrderDB
Backup -. snapshot .-> CatalogDB
Backup -. snapshot .-> PaymentLedger
Backup -. snapshot .-> AuditDB

OTel --> Dash
OTel --> Logs
OTel --> Trace
OTel --> Alerting
CI --> Canary
