"""
Endpoint configuration for the end-to-end suite.

The suite used to hardcode http://localhost:<port> and
postgresql://admin:supersecret@localhost:5432/... , so it could only ever run
against the compose stack. The Kubernetes deployment uses a generated Postgres
password from a Secret and reaches services by their in-cluster DNS names, so
verifying it meant running checks by hand -- which is how the cluster ended up
with no end-to-end coverage at all.

Three targets, selected with E2E_TARGET:

  compose  (default)  http://localhost:8005          postgres on localhost:5432
  cluster             http://catalog-service:8005    postgres on postgres:5432
                      -- run from inside the namespace
  forward             http://localhost:18005         postgres on localhost:15432
                      -- run from the host against kubectl port-forward

Examples:

    # compose, nothing to set
    python test_04_full_checkout_flow.py

    # inside the cluster
    E2E_TARGET=cluster PG_PASSWORD="$(kubectl -n ecommerce get secret \
      ecommerce-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)" \
      python test_04_full_checkout_flow.py

    # from the host, against port-forwards offset by 10000
    E2E_TARGET=forward PORT_OFFSET=10000 PG_PORT=15432 PG_PASSWORD=... \
      python test_04_full_checkout_flow.py

Any single endpoint can still be overridden outright, which is the escape
hatch when a target does not fit the pattern:

    E2E_URL_CATALOG_SERVICE=http://catalog.internal:8080
"""

import os

TARGET = os.getenv("E2E_TARGET", "compose").strip().lower()
if TARGET not in {"compose", "cluster", "forward"}:
    raise SystemExit(
        f"E2E_TARGET must be compose, cluster or forward (got {TARGET!r})")

# Offset applied to every service port in `forward` mode, so a wall of
# kubectl port-forwards can use 18005 for 8005 without per-service config.
PORT_OFFSET = int(os.getenv("PORT_OFFSET", "10000" if TARGET == "forward" else "0"))

# Canonical ports. These are the Rule 2 allocations and are identical in
# compose and Kubernetes, which is what makes one mapping serve both.
SERVICE_PORTS = {
    "api-gateway": 8000,
    "bff-shop": 8001,
    "bff-checkout": 8002,
    "websocket-gateway": 8003,
    "user-service": 8004,
    "catalog-service": 8005,
    "search-service": 8006,
    "cart-service": 8007,
    "pricing-service": 8008,
    "promotion-service": 8009,
    "tax-service": 8010,
    "delivery-quote-service": 8011,
    "order-saga": 8012,
    "inventory-service": 8013,
    "fraud-service": 8014,
    "payment-service": 8015,
    "fulfillment-service": 8016,
    "notification-service": 8017,
    "media-service": 8018,
    "audit-service": 8019,
    # The last slot in Rule 2's 8001-8020 core range.
    "seller-service": 8020,
    "reviews-service": 8022,
    "personalisation-service": 8023,
    # 8021 opens Rule 2's extension range; 8001-8020 is full.
    "bff-seller": 8021,
    "saga-dispatcher": 8030,
}

# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------
# The password has no default outside compose on purpose. Defaulting to
# "supersecret" against a cluster would fail with an authentication error that
# looks like a broken service rather than a missing variable.
PG_USER = os.getenv("PG_USER", "admin")
PG_HOST = os.getenv("PG_HOST", "postgres" if TARGET == "cluster" else "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_PASSWORD = os.getenv("PG_PASSWORD") or (
    "supersecret" if TARGET == "compose" else None)

ELASTICSEARCH_URL = os.getenv(
    "ELASTICSEARCH_URL",
    "http://elasticsearch:9200" if TARGET == "cluster" else "http://localhost:9200",
)


def _override(service: str):
    """E2E_URL_CATALOG_SERVICE beats everything else for catalog-service."""
    return os.getenv("E2E_URL_" + service.upper().replace("-", "_"))


# The seller seeded by migration 015: real, ACTIVE, contract v1 accepted.
#
# Tests that need *an* order but do not care whose used to mint a random UUID
# for seller_id. Nothing rejected it -- Rule 1 means catalog and order hold
# seller_id with no foreign key -- so those orders reached DELIVERED and emitted
# an escrow booking for a seller who had never existed. Each one was a
# permanent 409 that the dispatcher retried forever, and fourteen of them
# eventually filled the claim batch and starved real bookings out of the queue
# (ARCHITECTURE_STATE_FINAL.md §5c).
#
# The dispatcher now parks such commands instead of spinning on them, which is
# the real fix. This constant fixes the other half: a parked queue that always
# contains three fresh pieces of junk after every suite run is a parked queue
# nobody will read, and the whole point of parking was visibility.
PLATFORM_SELLER_ID = "00000000-0000-0000-0000-000000000001"


def service_url(service: str) -> str:
    """Base URL for a service, e.g. http://localhost:8005 -- no trailing slash."""
    explicit = _override(service)
    if explicit:
        return explicit.rstrip("/")

    try:
        port = SERVICE_PORTS[service]
    except KeyError:
        raise KeyError(
            f"unknown service {service!r}; add it to SERVICE_PORTS in config.py"
        ) from None

    if TARGET == "cluster":
        # In-cluster DNS: the Service name resolves inside the namespace and
        # listens on the same port compose publishes.
        return f"http://{service}:{port}"
    return f"http://localhost:{port + PORT_OFFSET}"


def db_url(database: str, driver: str = "postgresql") -> str:
    """DSN for one service database. `driver` allows postgresql+asyncpg."""
    if not PG_PASSWORD:
        raise SystemExit(
            f"PG_PASSWORD is required for E2E_TARGET={TARGET}. For the cluster:\n"
            "  PG_PASSWORD=$(kubectl -n ecommerce get secret ecommerce-secrets "
            "-o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)"
        )
    return f"{driver}://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{database}"


def describe() -> str:
    """One line for test output, so a run always says what it hit."""
    where = {
        "compose": "compose stack on localhost",
        "cluster": "Kubernetes, in-cluster DNS",
        "forward": f"Kubernetes via port-forward (+{PORT_OFFSET})",
    }[TARGET]
    return f"target={TARGET} ({where})  postgres={PG_HOST}:{PG_PORT}"


# ---------------------------------------------------------------------------
# in-mesh checks
# ---------------------------------------------------------------------------
NAMESPACE = os.getenv("E2E_NAMESPACE", "ecommerce")


def internal_url(service: str) -> str:
    """Address a service uses to reach a peer, from inside the mesh.

    Identical in both environments: compose resolves service names on its
    bridge network, Kubernetes resolves the Service name inside the namespace,
    and Rule 2 keeps the ports the same. Only the way in differs.
    """
    return f"http://{service}:{SERVICE_PORTS[service]}"


def mesh_exec_prefix(via: str = "api-gateway") -> list:
    """Command prefix that runs the rest of argv inside a mesh container."""
    if TARGET == "cluster" or TARGET == "forward":
        return ["kubectl", "-n", NAMESPACE, "exec", f"deploy/{via}", "--"]
    return ["docker", "exec", f"ecommerce-platform-{via}-1"]


# Compose runs a Redis primary, a replica and three sentinels. Which container
# is the primary is not fixed: sentinel does not fail back, so after
# test_10_redis_failover the node named `redis` is the replica and stays that
# way.
_REDIS_NODES = ("redis", "redis-replica")
_REDIS_SENTINELS = ("redis-sentinel-1", "redis-sentinel-2", "redis-sentinel-3")


def redis_primary_service(default: str = "redis") -> str:
    """The compose service name of the current Redis primary.

    Asked rather than assumed. A test that writes to `redis` by name gets
    "READONLY You can't write against a read only replica" once a failover has
    happened -- which is not a bug in the thing under test, but it fails the
    run and it does so only sometimes, depending on what ran before it.

    Falls back to `default` when there are no sentinels to ask: the Kubernetes
    dev cluster runs a single Redis on purpose (see
    infrastructure/k8s/dev-infra/infrastructure.yaml), and so does any
    deployment that leaves REDIS_SENTINELS empty.
    """
    if TARGET != "compose":
        return default

    import subprocess

    address = ""
    for sentinel in _REDIS_SENTINELS:
        result = subprocess.run(
            ["docker", "exec", f"ecommerce-platform-{sentinel}-1",
             "redis-cli", "-p", "26379", "sentinel",
             "get-master-addr-by-name", "mymaster"],
            capture_output=True, text=True, timeout=20)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            address = lines[0]
            break
    if not address:
        return default

    for node in _REDIS_NODES:
        result = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             f"ecommerce-platform-{node}-1"],
            capture_output=True, text=True, timeout=20)
        if result.stdout.strip() and result.stdout.strip() == address:
            return node
    return default


# ---------------------------------------------------------------------------
# a client that survives the machine it runs on
# ---------------------------------------------------------------------------
#
# Across four consecutive full-suite runs a *different* test failed each time,
# always with a transport error -- httpx.ReadError, httpx.ReadTimeout, or
# [WinError 64] "the specified network name is no longer available" -- and
# always passing when run on its own. Never an assertion. Never a wrong value.
#
# It is not the platform. One full run makes several thousand HTTP calls over
# about five minutes, and Docker Desktop on Windows drops published-port
# connections when the host is short of memory. A small per-call probability
# across thousands of calls is a near-certainty that *something* somewhere
# fails, which is why no single run came back clean while every individual test
# passed.
#
# The cost is not the red line. It is that a red suite stopped meaning "the
# platform is broken" and started meaning "the laptop hiccuped", and three
# separate real results were obscured by it in one day.
#
# So transport failures are retried and nothing else is. Specifically NOT
# retried:
#
#   * any HTTP status. A 500 is a real answer and the tests exist to see it.
#     Retrying until green is how a suite starts lying.
#   * assertion failures, which never reach this layer.
#
# This covers HTTP only. The suite also opens raw Postgres connections, and
# those turned out to be where both observed WinError 64s actually landed --
# see connect_with_retry below.

import httpx as _httpx

# Retried only for methods that can be repeated safely. GET/HEAD/OPTIONS always
# can. A mutation can only be retried if the platform will recognise the second
# attempt as the same one -- which is exactly Rule 4, so the presence of an
# Idempotency-Key is the condition, the same rule the services enforce.
_ALWAYS_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
TRANSPORT_RETRIES = 3
TRANSPORT_RETRY_BACKOFF = 0.5


class RetryingTransport(_httpx.AsyncBaseTransport):
    """Retries transport faults. Passes every HTTP response straight through."""

    def __init__(self, inner=None, retries=TRANSPORT_RETRIES,
                 backoff=TRANSPORT_RETRY_BACKOFF):
        self._inner = inner or _httpx.AsyncHTTPTransport()
        self._retries = retries
        self._backoff = backoff

    async def handle_async_request(self, request):
        import asyncio

        repeatable = (request.method in _ALWAYS_SAFE
                      or "idempotency-key" in request.headers)

        last = None
        for attempt in range(self._retries + 1):
            try:
                return await self._inner.handle_async_request(request)
            except _httpx.TransportError as exc:
                last = exc
                # A mutation with no idempotency key may already have taken
                # effect -- the response was lost, not the request. Retrying it
                # could place a second order, so it fails honestly instead.
                if not repeatable or attempt == self._retries:
                    raise
                await asyncio.sleep(self._backoff * (2 ** attempt))
        raise last  # unreachable; kept so the contract is explicit

    async def aclose(self):
        await self._inner.aclose()


def new_client(**kwargs):
    """An httpx.AsyncClient that rides out the host's connection drops.

    Use this instead of httpx.AsyncClient() anywhere in the suite. A default
    timeout is set because httpx's own default is None -- no timeout at all --
    which turns a dropped connection into a test that hangs rather than one
    that retries.
    """
    kwargs.setdefault("timeout", 30.0)
    kwargs.setdefault("transport", RetryingTransport())
    return _httpx.AsyncClient(**kwargs)


# ---------------------------------------------------------------------------
# the same problem, one layer down
# ---------------------------------------------------------------------------
#
# Wrapping httpx was only half of it, and the half that missed the evidence.
# Both [WinError 64] failures actually happened on raw Postgres connections --
# test_01 opening asyncpg mid-poll, and test_04 in its database bootstrap --
# where they surface as a bare ConnectionResetError out of the asyncio event
# loop rather than as an httpx.TransportError. The HTTP retry never saw them.
#
# Connecting is always safe to repeat: it is establishing a socket, not running
# a statement. So the retry wraps the *connect*, and nothing inside the
# session. A query that fails mid-transaction still fails, which is what the
# tests are there to notice.

# asyncio.TimeoutError is TimeoutError on 3.11+, but named explicitly so
# this keeps working if that ever stops being true.
import asyncio as _asyncio
_PG_CONNECT_ERRORS = (ConnectionResetError, ConnectionRefusedError,
                      ConnectionAbortedError, BrokenPipeError, OSError,
                      TimeoutError, _asyncio.TimeoutError)

PG_CONNECT_RETRIES = 3
PG_CONNECT_BACKOFF = 0.5

# asyncpg's connect has no timeout by default, and a retry loop is useless
# against a socket that never answers -- it blocks on the first attempt and
# never reaches the second. Observed exactly that: Docker Desktop's
# published-port forwarding hung while the container itself stayed healthy and
# served 39 connections internally, and a host-side connect sat for 60s+
# without raising. A bounded connect turns that hang into a retryable error,
# which is the whole point.
PG_CONNECT_TIMEOUT = 10.0


async def connect_with_retry(dsn, retries=PG_CONNECT_RETRIES,
                             backoff=PG_CONNECT_BACKOFF,
                             timeout=PG_CONNECT_TIMEOUT, **kwargs):
    """asyncpg.connect that rides out a dropped socket on the way up.

    Use this instead of asyncpg.connect() anywhere in the suite.

    Only the connection attempt is retried. Anything the caller then does with
    the connection is its own business and is not repeated -- re-running a
    statement after a mid-flight failure is how a test quietly does its work
    twice.
    """
    import asyncio
    import asyncpg

    last = None
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(
                asyncpg.connect(dsn, **kwargs), timeout=timeout)
        except _PG_CONNECT_ERRORS as exc:
            last = exc
            if attempt == retries:
                raise
            await asyncio.sleep(backoff * (2 ** attempt))
    raise last  # unreachable; kept so the contract is explicit


# ---------------------------------------------------------------------------
# and the third way the suite reaches the platform: docker exec
# ---------------------------------------------------------------------------
#
# I argued at first that retries did not belong here -- that a `docker exec`
# timing out meant the daemon was sick and wrapping it would be hiding a
# machine problem. That was right about a hung daemon and wrong about this.
#
# With Docker healthy, test_06 still failed once in a full run and passed
# immediately on its own. The budget was five seconds to spawn a process inside
# a container while twenty others were busy, which is simply too tight. A
# timeout that is wrong is not a machine that is broken.
#
# What makes retrying safe here is what these commands are: probes. `wget` at a
# health endpoint, `redis-cli ping`, reading a config. They observe and change
# nothing, so repeating one cannot do anything twice -- the same test that
# `new_client` applies to GET and `connect_with_retry` applies to connecting.
#
# Commands that DO change something -- killing a broker in test_09, forcing a
# failover in test_10 -- must not come through here. They are the experiment,
# and silently repeating an experiment is not a retry, it is a different test.

EXEC_TIMEOUT = 20
EXEC_RETRIES = 2
EXEC_BACKOFF = 1.0


def run_probe(cmd, timeout=EXEC_TIMEOUT, retries=EXEC_RETRIES,
              backoff=EXEC_BACKOFF):
    """Run a read-only command, retrying if it times out.

    For probes only -- anything that observes without changing. A command with
    an effect must be run directly with subprocess so that a slow one fails
    loudly instead of being quietly repeated.

    Returns the CompletedProcess of the last attempt. A non-zero exit code is
    returned as-is and never retried: that is the command answering, and the
    caller is asking precisely because the answer matters.
    """
    import subprocess
    import time

    last = None
    for attempt in range(retries + 1):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            last = exc
            if attempt == retries:
                raise
            time.sleep(backoff * (2 ** attempt))
    raise last  # unreachable; kept so the contract is explicit
