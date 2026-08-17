# End-to-end suite

Runs against the compose stack or the Kubernetes deployment. Pick with
`E2E_TARGET`; everything else has a sensible default.

```bash
python tests/e2e/run_suite.py          # compose — the default, nothing to set
```

## Targets

| `E2E_TARGET` | Services reached as | Postgres | Run from |
|---|---|---|---|
| `compose` *(default)* | `http://localhost:8005` | `localhost:5432` | host |
| `cluster` | `http://catalog-service:8005` | `postgres:5432` | inside the namespace |
| `forward` | `http://localhost:18005` | `localhost:15432` | host, via `kubectl port-forward` |

Ports are the Rule 2 allocations and are identical in both environments, which
is what lets one mapping serve all three. `forward` adds `PORT_OFFSET`
(default 10000) so a wall of port-forwards needs no per-service config.

## Against the cluster

The cluster's Postgres password is generated into a Secret, so it must be
supplied — there is deliberately no default outside compose, because falling
back to `supersecret` produces an authentication error that reads like a broken
service rather than a missing variable.

```bash
export PG_PASSWORD=$(kubectl -n ecommerce get secret ecommerce-secrets \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)

# from the host, with port-forwards
kubectl -n ecommerce port-forward svc/postgres 15432:5432 &
for s in catalog-service:8005 pricing-service:8008 cart-service:8007 \
         bff-checkout:8002 order-saga:8012 search-service:8006; do
  name=${s%%:*}; port=${s##*:}
  kubectl -n ecommerce port-forward svc/$name $((port+10000)):$port &
done
E2E_TARGET=forward PG_PORT=15432 python tests/e2e/run_suite.py
```

## Overriding one endpoint

```bash
E2E_URL_CATALOG_SERVICE=http://catalog.internal:8080 python tests/e2e/run_suite.py
```

## What runs in CI

`test_07_cart_cache_coherence` and `test_08_rate_limit_state_loss` run on every
push, in `.github/workflows/stack-tests.yml`, alongside the concurrency and
storage checks in `tests/integration`. That workflow boots a seven-container
slice — postgres, redis, minio, cart-service, inventory-service, api-gateway,
audit-service — which is everything those tests touch.

`test_01` through `test_06` and `test_09` are **manual**. The first six drive
the CQRS pipeline (Debezium → Kafka → Elasticsearch) and the saga, so trimming
them is not possible: they need most of the platform or they are testing
nothing. `test_09_broker_loss` needs the Kafka layer specifically — three
brokers plus ZooKeeper — and stops one of them mid-run. The full stack is 36
containers including seven JVMs and wants roughly 8 GB, while a GitHub-hosted
standard runner on a private repository is 2 cores and 8 GB.

`test_09` is also the one test that is deliberately **not** portable: it stops
and starts brokers by container name, so it only runs against compose. The
Kubernetes dev cluster runs a single broker on purpose (see
`infrastructure/k8s/dev-infra/kafka.yaml`), which means there is nothing there
for it to assert.

Two ways to automate the rest, if it becomes worth it:

- a **self-hosted runner** on a machine that already runs the stack — free, and
  the bring-up is exactly `make init`;
- **larger GitHub runners**, which are billed per minute.

Until then, run them by hand after any change to the CQRS or saga paths:

```bash
python tests/e2e/run_suite.py
```

## Notes

- Order matters: `test_02` and `test_03` read the product id `test_01` writes to
  `.e2e_state.json`.
- `test_06` checks connectivity from *inside* the mesh. The addresses are the
  same in both environments; only the way in differs, and `mesh_exec_prefix()`
  swaps `docker exec` for `kubectl exec` automatically.
- A green run against the wrong environment is worse than a red one, so every
  run prints its target first.
