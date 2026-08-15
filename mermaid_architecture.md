---
config:
  layout: elk
---
flowchart TD
%% =========================
%% 1. EDGE & EXPERIENCE
%% =========================
subgraph Edge["1. Edge & Experience Layer"]
  Client[Web / Mobile Apps]
  Admin[Admin / Ops Portal]
  WAF["WAF / CDN<br/>(DDoS, Bot Control)"]
  Ingress["Ingress Gateway<br/>(JWT, Tiered Rate Limit, OpenAPI Validation)"]
  BFFShop["Shop BFF<br/>(Read-Heavy, Circuit Breaker)"]
  BFFCheckout["Checkout BFF<br/>(Write-Heavy, Circuit Breaker)"]
  WSGW["Managed WebSocket Gateway<br/>(Redis Pub/Sub, Reconnect)"]
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
  Redis[(Redis Cache / Session Store)]

  CatalogSvc[Catalog Svc]
  CatalogDB[(Catalog DB)]

  SearchSvc[Search Query Svc]
  SearchDB[(Search Index / Read Store)]

  CartSvc["Cart Svc<br/>(Checkout Mutex)"]
  CartDB[(Cart DB)]

  PricingSvc[Pricing Svc]
  PromoSvc[Promotion Svc]
  TaxSvc[Tax Svc]
  DeliveryQuoteSvc[Delivery Quote Svc]

  UserSvc[User / Profile Svc]
  UserDB[(User DB)]

  OrderSaga["Order Saga Orchestrator<br/>(State Machine + Timeout Reaper)"]
  SagaReaper["Saga Reaper<br/>(Staleness Sweep, 60s interval)"]
  OrderDB[(Order DB)]

  InventorySvc[Inventory Svc]
  InventoryDB[(Inventory DB)]

  FraudSvc["Fraud / Risk Svc<br/>(Velocity: User+IP+DeviceFP)"]

  PaymentSvc["Payment Svc<br/>(PCI Tokenized)"]
  PaymentLedger[(Payment Ledger DB)]

  NotificationSvc[Notification Svc]
  NotificationDB[(Notification DB)]

  FulfillmentSvc[Fulfillment Svc]
  FulfillmentDB[(Fulfillment DB)]
end

%% =========================
%% 4. EVENT MESH
%% =========================
subgraph Events["4. Event Mesh (Strict Correctness)"]
  CDC["Outbox / CDC<br/>(Debezium)"]
  Kafka[[Kafka Cluster]]
  Registry["Schema Registry<br/>(Avro / Protobuf, FULL_TRANSITIVE)"]
  DLQ[["Dead Letter Queues<br/>(Alerting)"]]
  DLQReprocessor["DLQ Reprocessor<br/>(Backoff Retry x3, then Escalate)"]
end

%% =========================
%% 5. SEARCH INDEXING
%% =========================
subgraph SearchPipe["5. Search Indexing & Read Models"]
  Stream["Stream Processor<br/>(Kafka Streams / Flink)"]
  Reindex[Backfill / Reindex Job]
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
  NotifyWorker[Notification Workers]
  Email[Email Provider]
  SMS[SMS Provider]
  Push[Push Provider]
  PSP[External Payment Gateway / PSP]
  Webhooks["Webhook Handler<br/>(4-Layer Dedup)"]
  Courier[Shipping / Courier API]
  Audit[Immutable Audit Log]
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

%% =========================
%% CONNECTIONS
%% =========================
Client -. Auto-reconnect .-> WSGW
Admin --> Ingress

Ingress --> IAM
Config -. config .-> Ingress
Autoscale -. scales .-> Ingress

BFFShop -->|Circuit Breaker| CartSvc
BFFShop -->|Circuit Breaker| CatalogSvc
BFFShop -->|Circuit Breaker| SearchSvc
BFFShop -->|Circuit Breaker| PricingSvc
BFFShop -->|Circuit Breaker| PromoSvc
BFFShop -->|Circuit Breaker| UserSvc

BFFCheckout -->|Circuit Breaker| CartSvc
BFFCheckout -->|Circuit Breaker| OrderSaga
BFFCheckout -->|Circuit Breaker| TaxSvc
BFFCheckout -->|Circuit Breaker| DeliveryQuoteSvc
BFFCheckout -->|Circuit Breaker| FraudSvc
BFFCheckout -->|Circuit Breaker| PaymentSvc

CartSvc --> Redis
WSGW --> Redis

CatalogSvc --> CatalogDB
SearchSvc --> SearchDB
CartSvc --> CartDB
UserSvc --> UserDB
OrderSaga --> OrderDB
SagaReaper --> OrderDB
InventorySvc --> InventoryDB
PaymentSvc --> PaymentLedger
NotificationSvc --> NotificationDB
FulfillmentSvc --> FulfillmentDB

OrderSaga -->|1. Reserve inventory| InventorySvc
OrderSaga -->|2. Price / Tax / Ship quote| PricingSvc
OrderSaga --> TaxSvc
OrderSaga --> DeliveryQuoteSvc
OrderSaga -->|3. Apply promo rules| PromoSvc
OrderSaga -->|4. Risk check| FraudSvc
OrderSaga -->|5. Charge payment| PaymentSvc
OrderSaga -->|6. Confirm / compensate| InventorySvc
OrderSaga -->|7. Dispatch order| FulfillmentSvc

SagaReaper -.->|Compensate stuck sagas| OrderSaga

Backup -. snapshot .-> OrderDB
Backup -. snapshot .-> CatalogDB
Backup -. snapshot .-> PaymentLedger

CatalogDB --> CDC
CartDB --> CDC
OrderDB --> CDC
InventoryDB --> CDC
PaymentLedger --> CDC
UserDB --> CDC
NotificationDB --> CDC
FulfillmentDB --> CDC

CDC --> Kafka
Kafka <--> Registry
Kafka -. Poison pills .-> DLQ
DLQ --> DLQReprocessor
DLQReprocessor -.->|Retry up to 3x| Kafka
DLQReprocessor -.->|Escalate after 3x| Audit
DLQ -.->|Alert within 5min| Alerting

Kafka --> Stream
Stream --> Elastic
CDC --> Reindex
Reindex --> Elastic
SearchSvc --> Elastic

Client --> Upload
Upload --> Quarantine
Quarantine --> Scanner
Scanner --> Clean
Clean --> CDN
Scanner --> MediaMeta

Kafka --> NotifyWorker
NotifyWorker --> Email
NotifyWorker --> SMS
NotifyWorker --> Push

PaymentSvc --> PSP
PSP --> Webhooks
Webhooks --> PaymentSvc

DeliveryQuoteSvc --> Courier
FulfillmentSvc --> Courier
Vault --> Audit

OTel --> Dash
OTel --> Logs
OTel --> Trace
OTel --> Alerting
OTel --> CI
OTel --> Canary
