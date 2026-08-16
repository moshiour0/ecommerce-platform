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

## Notes

- Order matters: `test_02` and `test_03` read the product id `test_01` writes to
  `.e2e_state.json`.
- `test_06` checks connectivity from *inside* the mesh. The addresses are the
  same in both environments; only the way in differs, and `mesh_exec_prefix()`
  swaps `docker exec` for `kubectl exec` automatically.
- A green run against the wrong environment is worse than a red one, so every
  run prints its target first.
