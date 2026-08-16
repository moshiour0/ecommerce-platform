# Integration checks

Three concurrency checks that need the stack up. Each one asserts a property
that unit tests cannot reach: unit tests pin a decision against fake inputs,
these prove the decision survives real parallel traffic through real Redis and
real Postgres.

| Script | Asserts |
|---|---|
| `checkout_mutex.sh` | Exactly one of N parallel checkouts on one cart is accepted |
| `inventory_contention.sh` | Exactly STOCK of N parallel buyers are served — no oversell |
| `rate_limit_burst.sh` | The gateway caps a burst at its budget, and `/health` survives an exhausted budget |

They run inside the mesh in a `curlimages/curl` container, because no service
image ships curl.

## Running them

The invocation differs by shell, and not arbitrarily — pick the one for the
shell you are in.

**PowerShell** — bind-mount the directory:

```powershell
docker run --rm --network ecommerce-platform_mesh -v "$PWD/tests/integration:/s:ro" curlimages/curl:8.5.0 sh /s/rate_limit_burst.sh
```

**Git Bash / WSL / Linux** — pipe the script in over stdin:

```bash
docker run --rm -i --network ecommerce-platform_mesh curlimages/curl:8.5.0 sh -s < tests/integration/rate_limit_burst.sh
```

Git Bash needs the stdin form because MSYS rewrites the *container* path in
`-v ...:/s:ro` into `S:/` before docker ever sees it, and the mount fails with a
"no such file or directory" that looks like the script is missing. Prefixing
with `MSYS_NO_PATHCONV=1` is the other way out.

`make rate-limit-check` and `make test-gateway` wrap these, but make is not
installed on every machine this repo is cloned onto — the commands above need
nothing but docker.

## Seeding

`checkout_mutex.sh` and `rate_limit_burst.sh` seed themselves.

`inventory_contention.sh` deliberately does not: it reserves against a product
row the caller created, so it never invents stock of its own. Against compose:

```powershell
$pid_ = [guid]::NewGuid().ToString()
docker exec ecommerce-platform-postgres-1 psql -U admin -d inventory_db -c "INSERT INTO inventory_items (id, product_id, quantity_available, quantity_reserved) VALUES (gen_random_uuid(), '$pid_', 5, 0);"
docker run --rm --network ecommerce-platform_mesh -e PRODUCT_ID=$pid_ -e STOCK=5 -e BUYERS=20 -v "$PWD/tests/integration:/s:ro" curlimages/curl:8.5.0 sh /s/inventory_contention.sh
```

`STOCK=3 BUYERS=30` is the setting that originally exposed the lock-convoy 500
storm, so it is the more interesting run of the two.

## Tuning

Every script reads its parameters from the environment: `CONCURRENCY`,
`STOCK`/`BUYERS`/`PRODUCT_ID`, and `BUDGET`/`REQUESTS`/`GATEWAY_URL`/
`PROBE_PATH` respectively.

`rate_limit_burst.sh` keys on the caller's IP, and the mesh recycles container
addresses. A run that lands on an address still inside the previous run's 60s
window starts from a used counter, so fewer requests are admitted — which is why
it asserts *at most* the budget rather than exactly it. Over-serving is the
failure; being stricter than advertised is not. To watch the counters directly:

```powershell
docker exec ecommerce-platform-redis-1 redis-cli --scan --pattern "rl:*"
```
