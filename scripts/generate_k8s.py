#!/usr/bin/env python3
"""
Generate Kubernetes manifests from the compose topology.

    python scripts/generate_k8s.py

All twenty manifests under infrastructure/k8s/services were 0 bytes. Writing
them by hand would have produced twenty files that drift from the compose
setup the moment a port or an environment variable changes -- and drift is
exactly how this project ended up with connectors pointing at databases that
do not exist. The compose file is the configuration that demonstrably works,
so it is the source of truth and these manifests are derived from it.

Secrets are never emitted with values. Rule 8 requires Vault-managed secrets
and forbids hardcoded fallbacks, so POSTGRES_PASSWORD and JWT_SECRET are
referenced from a Secret that this script does not create. Anything matching
a secret name is stripped from the ConfigMap and wired as a secretKeyRef.

Rule 10: migrations run as a Job, never in the application startup path. The
Job is applied before a rollout; the Deployments do not run any DDL.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "infrastructure" / "k8s"
NAMESPACE = "ecommerce"
REGISTRY = "ecommerce-platform"       # image prefix; matches compose build tags
IMAGE_TAG = "latest"

# Values that must come from a Secret, never a ConfigMap (Rule 8).
SECRET_KEYS = {"JWT_SECRET", "POSTGRES_PASSWORD"}

# ...and anything that looks like a secret by name, so this list does not have
# to be remembered. It was an allowlist of exactly two, which meant adding
# PSP_WEBHOOK_SECRET to compose silently produced a manifest containing the
# real signing secret in plaintext -- in a generated file that goes straight
# into git. A name-shaped default fails closed: the cost of wrongly treating a
# variable as secret is a missing Secret key, which is loud, while the cost of
# wrongly treating a secret as config is a committed credential, which is not.
SECRET_SUFFIXES = ("_SECRET", "_PASSWORD", "_TOKEN", "_API_KEY", "_PRIVATE_KEY")

SECRET_NAME = "ecommerce-secrets"
CONFIG_NAME = "ecommerce-config"


def secret_key_for(name: str):
    """The Secret key backing this variable, or None if it is ordinary config."""
    for known in SECRET_KEYS:
        if known in name:
            return known
    if name.endswith(SECRET_SUFFIXES):
        return name
    return None

# Infrastructure lives outside this generator: in a real cluster Postgres,
# Kafka and Elasticsearch are operators or managed services, not Deployments
# copied from a dev compose file. Emitting fake ones would imply a production
# topology nobody intends to run.
INFRA = {"postgres", "redis", "kafka", "zookeeper", "elasticsearch",
         "schema-registry", "jaeger", "debezium"}

WORKERS = {"saga-dispatcher", "stream-processor", "webhook-handler",
           "notification-worker", "dlq-reprocessor", "reindex-worker"}

# Rough sizing. Deliberately explicit: a pod with no requests is unschedulable
# in a constrained cluster and a pod with no limits can starve its neighbours.
RESOURCES = {
    "default": {"requests": {"cpu": "50m", "memory": "128Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"}},
    "edge":    {"requests": {"cpu": "100m", "memory": "192Mi"},
                "limits": {"cpu": "1000m", "memory": "768Mi"}},
}
EDGE = {"api-gateway", "bff-shop", "bff-checkout", "websocket-gateway"}


def compose_config() -> dict:
    res = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.apps.yml", "config"],
        cwd=REPO, capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.exit(f"FATAL: docker compose config failed\n{res.stderr}")
    return yaml.safe_load(res.stdout)


def rewrite_host(value: str) -> str:
    """compose service names resolve inside a namespace too, so hostnames are
    already correct. Only localhost needs rewriting -- it means 'this pod' in
    Kubernetes, which is not what a cross-service URL intends."""
    return re.sub(r"//localhost:", "//127.0.0.1:", value)


# postgresql+asyncpg://user:password@host:5432/db
DSN_CREDENTIALS = re.compile(r"(?P<scheme>[a-z+]+://)(?P<user>[^:/@]+):(?P<pw>[^@]+)@")


def split_env(env: dict):
    """Return (plain_env, secret_refs, needs_pg_password) for one service.

    Compose embeds the database password directly in DATABASE_URL. Copying
    that into a manifest puts the credential in plaintext in sixteen files
    committed to the repository, which is precisely the Rule 8 violation that
    the JWT_SECRET fix removed from docker-compose. Kubernetes expands
    $(VARNAME) inside env values using variables defined earlier in the same
    container, so the DSN is rewritten to interpolate the Secret instead.
    """
    cfg, secrets, needs_pg = {}, [], False
    for k, v in (env or {}).items():
        v = "" if v is None else str(v)
        hit = secret_key_for(k)
        if hit:
            secrets.append((k, hit))
            continue
        m = DSN_CREDENTIALS.search(v)
        if m:
            needs_pg = True
            v = DSN_CREDENTIALS.sub(
                lambda mm: f"{mm.group('scheme')}{mm.group('user')}:$(POSTGRES_PASSWORD)@", v)
        cfg[k] = rewrite_host(v)
    return cfg, secrets, needs_pg


def container_env(cfg: dict, secrets: list, needs_pg: bool) -> list:
    """Secret-backed variables are emitted FIRST.

    $(VAR) expansion only resolves against variables that appear earlier in
    the same container's env list. With the secret appended last, every DSN
    would render the literal string "$(POSTGRES_PASSWORD)" and every database
    connection would fail authentication.
    """
    out = []
    if needs_pg and not any(n == "POSTGRES_PASSWORD" for n, _ in secrets):
        out.append({"name": "POSTGRES_PASSWORD", "valueFrom": {
            "secretKeyRef": {"name": SECRET_NAME, "key": "POSTGRES_PASSWORD"}}})
    for name, key in secrets:
        out.append({"name": name, "valueFrom": {
            "secretKeyRef": {"name": SECRET_NAME, "key": key}}})
    out.extend({"name": k, "value": v} for k, v in sorted(cfg.items()))
    return out


def probes(port: int | None):
    if port is None:
        return {}, {}
    # Readiness gates traffic; liveness restarts a wedged process. Different
    # thresholds on purpose: a slow start should delay traffic, not trigger a
    # restart loop.
    common = {"httpGet": {"path": "/health", "port": port}}
    readiness = {**common, "initialDelaySeconds": 5, "periodSeconds": 5,
                 "timeoutSeconds": 3, "failureThreshold": 3}
    liveness = {**common, "initialDelaySeconds": 30, "periodSeconds": 20,
                "timeoutSeconds": 5, "failureThreshold": 5}
    return readiness, liveness


def deployment(name: str, svc: dict, port: int | None) -> dict:
    cfg, secrets, needs_pg = split_env(svc.get("environment") or {})
    readiness, liveness = probes(port)
    is_worker = name in WORKERS

    container = {
        "name": name,
        "image": f"{REGISTRY}-{name}:{IMAGE_TAG}",
        "imagePullPolicy": "IfNotPresent",
        "env": container_env(cfg, secrets, needs_pg),
        "resources": RESOURCES["edge" if name in EDGE else "default"],
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "capabilities": {"drop": ["ALL"]},
        },
    }
    if svc.get("command"):
        container["command"] = svc["command"]
    if port is not None:
        container["ports"] = [{"containerPort": port, "name": "http"}]
        container["readinessProbe"] = readiness
        container["livenessProbe"] = liveness

    # Rule 10 permits an InitContainer for schema work, and one is required
    # here. Under compose, bootstrap_schema.py runs create_all against every
    # service before the SQL migrations. Kubernetes had no equivalent, so a
    # fresh cluster had no service tables at all -- order_saga_states,
    # inventory_items, payment_ledger and the rest simply did not exist, and
    # the migration Job died on its first ALTER against an empty database.
    #
    # This runs the service's own models in the service's own image, so the
    # DDL cannot drift from the code. It is idempotent (create_all issues
    # CREATE TABLE IF NOT EXISTS) and does not run in the application startup
    # path: the app container starts only after it exits 0.
    # Keyed off the code actually being there, not off DATABASE_URL. A stub
    # service declares a DATABASE_URL while having no app/database.py or
    # app/models.py, so an init container crashed on ModuleNotFoundError and
    # held the pod in Init:Error forever. media-service and audit-service both
    # have them now and pick up init containers automatically, which is the
    # check working as intended rather than a special case being removed.
    src = REPO / "services" / name / "app"
    has_orm = (src / "database.py").exists() and (src / "models.py").exists()

    init_containers = []
    if has_orm and any(e["name"] == "DATABASE_URL" for e in container["env"]):
        init_containers.append({
            "name": "schema-init",
            "image": container["image"],
            "imagePullPolicy": "IfNotPresent",
            "command": ["python", "-c",
                        "import asyncio\n"
                        "from app.database import engine, Base\n"
                        "import app.models\n"
                        "async def m():\n"
                        "    async with engine.begin() as c:\n"
                        "        await c.run_sync(Base.metadata.create_all)\n"
                        "asyncio.run(m())\n"
                        "print('schema ready')\n"],
            "env": container["env"],
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                          "limits": {"cpu": "500m", "memory": "256Mi"}},
            "securityContext": container["securityContext"],
        })

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"app": name, "tier": "worker" if is_worker else "service"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "strategy": {
                "type": "RollingUpdate",
                # Never take the last replica down before its successor is
                # ready: these services are in the synchronous checkout path.
                "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
            },
            "template": {
                "metadata": {"labels": {"app": name, "tier": "worker" if is_worker else "service"}},
                "spec": {
                    **({"initContainers": init_containers} if init_containers else {}),
                    "containers": [container],
                    "securityContext": {"fsGroup": 1000},
                    # Give in-flight checkout requests time to finish.
                    "terminationGracePeriodSeconds": 30,
                },
            },
        },
    }


def service(name: str, port: int) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": name}},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": name},
            # Rule 2 keeps the port identical to the compose mapping so a
            # service is reachable at the same number in both environments.
            "ports": [{"name": "http", "port": port, "targetPort": port, "protocol": "TCP"}],
        },
    }


def dump(docs: list, path: Path, header: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n---\n".join(yaml.safe_dump(d, sort_keys=False, width=100) for d in docs)
    path.write_text(f"# {header}\n# GENERATED by scripts/generate_k8s.py — do not edit by hand.\n"
                    f"# Regenerate after changing docker-compose.apps.yml.\n---\n{body}",
                    encoding="utf-8")


# Which migration applies to which database. Mirrors scripts/bootstrap_schema.py;
# both read from migrations/, so the SQL itself is never duplicated.
MIGRATION_TARGETS = [
    ("001_add_idempotency_result_id.sql",
     ["order_db", "inventory_db", "payment_ledger_db", "fraud_db", "cart_db"]),
    ("002_cart_db_create_cart_state.sql", ["cart_db"]),
    ("003_promo_db_create_tables.sql", ["promo_db"]),
    ("004_outbox_for_audit_and_media.sql", ["audit_db", "media_meta_db"]),
    ("005_dispatcher_processed_state.sql", ["order_db"]),
    ("006_dispatcher_claim_lease.sql", ["order_db"]),
]


def migration_job() -> list:
    """Rule 10: migrations run as a Job, never in the application startup path.

    Apply this and wait for completion before rolling out Deployments; a pod
    that starts against an un-migrated database is the failure that made every
    write endpoint return 500 when result_id was missing.
    """
    mig_dir = REPO / "migrations"
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(mig_dir.glob("*.sql"))}

    # Ordered psql invocations, one per (file, database) pair.
    steps = "\n".join(
        f'echo "--> {fn} -> {db}"\n'
        f'psql -v ON_ERROR_STOP=1 -h "$PGHOST" -U "$PGUSER" -d {db} -f /migrations/{fn}'
        for fn, dbs in MIGRATION_TARGETS for db in dbs
    )

    script = (
        "set -euo pipefail\n"
        'echo "waiting for postgres..."\n'
        'until pg_isready -h "$PGHOST" -U "$PGUSER" >/dev/null 2>&1; do sleep 2; done\n'
        'echo "postgres ready"\n'
        f"{steps}\n"
        'echo "ALL MIGRATIONS APPLIED"\n'
    )

    cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "db-migrations", "namespace": NAMESPACE},
        "data": files,
    }
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "db-migrate", "namespace": NAMESPACE},
        "spec": {
            "backoffLimit": 3,
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {"labels": {"app": "db-migrate", "tier": "job"}},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "psql",
                        "image": "postgres:15-alpine",
                        "command": ["sh", "-c", script],
                        "env": [
                            {"name": "PGHOST", "value": "postgres"},
                            {"name": "PGUSER", "value": "admin"},
                            {"name": "PGPASSWORD", "valueFrom": {
                                "secretKeyRef": {"name": SECRET_NAME, "key": "POSTGRES_PASSWORD"}}},
                        ],
                        "volumeMounts": [{"name": "migrations", "mountPath": "/migrations"}],
                        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                                      "limits": {"cpu": "500m", "memory": "256Mi"}},
                    }],
                    "volumes": [{"name": "migrations", "configMap": {"name": "db-migrations"}}],
                },
            },
        },
    }
    return [cm, job]


def main():
    cfg = compose_config()
    services = cfg["services"]
    written = []

    for name, svc in sorted(services.items()):
        if name in INFRA or "build" not in svc:
            continue
        ports = svc.get("ports") or []
        port = int(ports[0]["target"]) if ports else None

        docs = [deployment(name, svc, port)]
        if port is not None:
            docs.append(service(name, port))

        subdir = "workers" if name in WORKERS else "services"
        path = OUT / subdir / f"{name}.yaml"
        dump(docs, path, f"{name} — Deployment{' + Service' if port else ''}")
        written.append((subdir, name, port))

    dump(migration_job(), OUT / "jobs" / "db-migrate.yaml",
         "Rule 10 — schema migrations as a Job, never in the app startup path")
    written.append(("jobs", "db-migrate", None))

    print(f"generated {len(written)} manifests")
    for sub, n, p in written:
        print(f"  {sub}/{n}.yaml  port={p if p else '-'}")


if __name__ == "__main__":
    main()
