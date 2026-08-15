# Kubernetes manifests

Generated from the compose topology. **Do not edit the files under
`services/`, `workers/` or `jobs/` by hand** — regenerate instead:

```bash
python scripts/generate_k8s.py    # rebuild from docker-compose.apps.yml
python scripts/validate_k8s.py    # cross-check against compose, no cluster needed
```

The compose configuration is the source of truth because it is the one that
demonstrably works: 30 containers, 15/15 CDC connectors, e2e 6/6. Hand-written
manifests would drift from it the moment a port or an environment variable
changed, and drift is how this project previously ended up with Debezium
connectors pointing at databases that did not exist.

## What is here

| Path | Contents |
|---|---|
| `base/namespace.yaml` | Namespace, default-deny NetworkPolicy, mesh injection label |
| `base/secrets.example.yaml` | Secret **shape** only — no values, ever |
| `jobs/db-migrate.yaml` | Schema migrations as a Job (Rule 10) |
| `services/*.yaml` | 20 Deployments + Services |
| `workers/*.yaml` | `saga-dispatcher`, `stream-processor` |

## Apply order

Ordering is not incidental. A pod that starts against an un-migrated database
is exactly the failure that made every write endpoint return HTTP 500 when
`idempotency_keys.result_id` was missing.

```bash
# 1. namespace and network policy
kubectl apply -f infrastructure/k8s/base/namespace.yaml

# 2. secrets — never from the example file; keep values out of the repo
kubectl -n ecommerce create secret generic ecommerce-secrets \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)"

# 3. migrations, and WAIT for completion before any rollout (Rule 10)
kubectl apply -f infrastructure/k8s/jobs/db-migrate.yaml
kubectl -n ecommerce wait --for=condition=complete job/db-migrate --timeout=300s

# 4. workloads
kubectl apply -f infrastructure/k8s/services/
kubectl apply -f infrastructure/k8s/workers/
```

## What these manifests do NOT include

Stated explicitly so nobody assumes coverage that is not there.

- **Infrastructure.** No Postgres, Kafka, Zookeeper, Elasticsearch, Redis,
  Schema Registry, Debezium or Jaeger. In a real cluster these are operators
  or managed services, not Deployments copied from a dev compose file.
  Emitting them would imply a production topology nobody intends to run.
  Every workload here expects to reach them at their compose service names.
- **Ingress.** `api-gateway` is a ClusterIP. Exposing it needs an Ingress or
  Gateway API resource matched to your controller, plus TLS.
- **HPA / KEDA.** The architecture diagram calls for autoscaling; none is
  defined. Replica counts are all 1.
- **Per-workload NetworkPolicies.** The namespace denies ingress by default
  and then allows anything inside the namespace. Narrowing that to the actual
  caller of each service requires encoding the Rule 7 BFF/saga boundary per
  workload.
- **PodDisruptionBudgets, anti-affinity, topology spread.** All single-replica.

## Verification status

`scripts/validate_k8s.py` checks coverage against compose, port agreement
across container/Service/compose, Service-selector-to-pod-label matching,
`$(VAR)` expansion ordering, probes, resource requests and limits, and the
absence of plaintext credentials.

Running as UID 1000 was verified for real: both a Python and a Node image were
started with `--user 1000:1000` and came up clean, so `runAsNonRoot` is an
accurate claim rather than an aspirational one.

**These manifests have never been applied to a cluster.** No Kubernetes was
available in the development environment, and `kubectl` cannot validate
offline — it downloads the OpenAPI schema from the API server. Nothing here
has been checked by an admission controller, and no image has been pulled by a
kubelet. Treat a first `kubectl apply` as an untested step.
