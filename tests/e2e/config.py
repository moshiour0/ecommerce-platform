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
