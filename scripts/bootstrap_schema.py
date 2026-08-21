#!/usr/bin/env python3
"""
Bring every database to the current schema, from empty.

Two phases, in order:

  1. Model DDL   -- run Base.metadata.create_all inside each service container,
                    so the DDL comes from the same SQLAlchemy models the
                    service will use and from an environment with the right
                    dependencies installed.
  2. Migrations  -- apply migrations/*.sql to their target databases. These
                    carry changes that live outside the models: columns and
                    tables the raw-SQL paths need (processed_events,
                    outbox_messages.processed_at, claimed_at/claimed_by).

Why this exists
---------------
Schema state used to survive only inside a long-lived Postgres volume. Every
fix applied by hand with `docker exec` vanished on `make clean`
(`docker-compose down -v`), and nothing recreated it. The predecessor,
init_schemas.py, could not close that gap:

  * it pointed promotion-service at `promotion_db`, which does not exist
    (the database is `promo_db`), so that service silently got no tables;
  * it omitted media-service and audit-service entirely;
  * it wrapped every service in `except Exception: print(...)` and still
    printed "DEPLOYMENT COMPLETE", so a total failure looked like a success.

This script exits non-zero the moment anything fails.

Usage:  python scripts/bootstrap_schema.py
"""

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PG = "ecommerce-platform-postgres-1"
PSQL = ["docker", "exec", "-i", PG, "psql", "-U", "admin", "-v", "ON_ERROR_STOP=1", "-q"]

# service directory -> (container name, database). Databases are the real ones
# created by init-dbs.sh; deriving them by stripping "_db" is what produced the
# promotion_db / payment_db / media_db bugs.
SERVICES = {
    "catalog-service":        "catalog_db",
    "user-service":           "user_db",
    "pricing-service":        "pricing_db",
    "promotion-service":      "promo_db",
    "tax-service":            "tax_db",
    "cart-service":           "cart_db",
    "inventory-service":      "inventory_db",
    "fraud-service":          "fraud_db",
    "payment-service":        "payment_ledger_db",
    "order-saga":             "order_db",
    "delivery-quote-service": "delivery_quote_db",
    "fulfillment-service":    "fulfillment_db",
    "notification-service":   "notification_db",
    "media-service":          "media_meta_db",
    "seller-service":         "seller_db",
    "audit-service":          "audit_db",
}

# Each migration names the databases it targets. One file per database concern:
# applying a file to the wrong database creates tables a service does not own,
# which violates Rule 1.
MIGRATIONS = [
    ("001_add_idempotency_result_id.sql",
     ["order_db", "inventory_db", "payment_ledger_db", "fraud_db", "cart_db"]),
    ("002_cart_db_create_cart_state.sql", ["cart_db"]),
    ("003_promo_db_create_tables.sql", ["promo_db"]),
    ("004_outbox_for_audit_and_media.sql", ["audit_db", "media_meta_db"]),
    ("005_dispatcher_processed_state.sql", ["order_db"]),
    ("006_dispatcher_claim_lease.sql", ["order_db"]),
    ("007_payment_db_processed_webhooks.sql", ["payment_ledger_db"]),
    ("008_notification_db_delivery_state.sql", ["notification_db"]),
    ("009_catalog_db_sku_and_is_active.sql", ["catalog_db"]),
    ("010_cart_db_cart_state_updated_at.sql", ["cart_db"]),
    ("011_inventory_db_reservations.sql", ["inventory_db"]),
    ("012_catalog_db_seller_id.sql", ["catalog_db"]),
    ("013_media_db_asset_purpose.sql", ["media_meta_db"]),
    ("014_seller_db_onboarding.sql", ["seller_db"]),
    ("015_seller_db_platform_seller.sql", ["seller_db"]),
    ("016_order_db_seller_orders.sql", ["order_db"]),
    ("017_order_db_cod_lifecycle.sql", ["order_db"]),
    ("018_fulfillment_db_shipments.sql", ["fulfillment_db"]),
    ("019_payment_db_escrow_ledger.sql", ["payment_ledger_db"]),
]

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    filename    text                     NOT NULL PRIMARY KEY,
    applied_at  timestamp with time zone NOT NULL DEFAULT NOW()
);
"""

failures = []


def run(cmd, **kw):
    # UTF-8 explicitly, in both directions.
    #
    # `text=True` alone encodes stdin with the *locale* encoding, which is
    # cp1252 on a Windows host. A migration containing any character outside
    # ASCII then reaches psql as cp1252 bytes while psql is decoding UTF-8, and
    # it fails with `invalid byte sequence for encoding "UTF8"` pointing at a
    # line that looks perfectly ordinary. Migration 016 hit exactly that on a
    # section-sign in a comment.
    kw.setdefault("env", {**os.environ, "PGCLIENTENCODING": "UTF8"})
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def wait_for_postgres(timeout=90):
    print("Waiting for Postgres...")
    for _ in range(timeout):
        if run(["docker", "exec", PG, "pg_isready", "-U", "admin"]).returncode == 0:
            print("  Postgres ready\n")
            return
        time.sleep(1)
    sys.exit("FATAL: Postgres did not become ready")


def psql(db, sql=None, file=None):
    cmd = PSQL + ["-d", db]
    if file:
        return run(cmd + ["-f", "-"], input=Path(file).read_text(encoding="utf-8"))
    return run(cmd + ["-c", sql])


def phase_databases():
    """Create any service database that does not exist yet.

    init-dbs.sh only runs on a *fresh* Postgres volume. Adding a database to
    it therefore does nothing on any machine that already has data -- which is
    every machine the platform has ever run on. Adding seller_db hit exactly
    that: the file was right, the database was absent, and the service could
    not connect.

    The alternative was a line in the README telling people to run CREATE
    DATABASE by hand, which is a step that gets skipped and an error that
    reads like a bug in the service. Creating them here means the file and the
    server agree after one command, on a fresh volume and an old one alike.

    CREATE DATABASE cannot run inside a transaction block, so this shells out
    per database rather than batching.
    """
    print("PHASE 0 — service databases")
    existing = run(PSQL + ["-d", "postgres", "-tAc",
                           "SELECT datname FROM pg_database"])
    present = {line.strip() for line in existing.stdout.splitlines() if line.strip()}

    for service, db in sorted(SERVICES.items(), key=lambda kv: kv[1]):
        if db in present:
            continue
        res = run(PSQL + ["-d", "postgres", "-c", f'CREATE DATABASE {db}'])
        if res.returncode == 0:
            print(f"  CREATED {db}  (for {service})")
        else:
            err = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["unknown"]
            print(f"  FAIL    {db}: {err[0][:120]}")
            failures.append(f"create database {db}")
    print()


def phase_models():
    """Run create_all in each present service. Returns the databases it covered.

    The returned set is what makes a partial bring-up verifiable. CI boots a
    five-container slice rather than the whole platform, so most services are
    legitimately absent, and asserting tables that only their models create
    would fail a run that is in fact correct. With the full stack up every
    service is present and the set is complete, so nothing changes there.
    """
    print("PHASE 1 — model DDL (create_all inside each service container)")
    live_dbs = set()
    for service, db in SERVICES.items():
        container = f"ecommerce-platform-{service}-1"
        if run(["docker", "inspect", "-f", "{{.State.Status}}", container]).returncode != 0:
            print(f"  SKIP  {service:<24} container not present")
            continue
        if not (REPO / "services" / service / "app" / "models.py").exists():
            print(f"  SKIP  {service:<24} no models.py")
            continue
        res = run(["docker", "exec", container, "python", "-c",
                   "import asyncio\n"
                   "from app.database import engine, Base\n"
                   "import app.models\n"
                   "async def m():\n"
                   "    async with engine.begin() as c:\n"
                   "        await c.run_sync(Base.metadata.create_all)\n"
                   "asyncio.run(m())\n"])
        if res.returncode == 0:
            print(f"  OK    {service:<24} -> {db}")
            live_dbs.add(db)
        else:
            tail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["unknown error"]
            print(f"  FAIL  {service:<24} -> {db}: {tail[0][:120]}")
            failures.append(f"model DDL {service}")
    print()
    return live_dbs


def phase_migrations(live_dbs):
    """Apply migrations, but only to databases whose service actually ran.

    A migration that ALTERs a table created by a service's models cannot work
    when that service is absent -- 008 adds delivery columns to
    notification_records, which only exists once notification-service has run
    create_all. Against the CI slice that failed the whole bootstrap for a
    database nobody had brought up.

    Phase 1 already skips absent services and phase 3 skips their checks; this
    closes the middle, so a partial bring-up is coherent end to end. With the
    full stack every database is live and every migration runs, exactly as
    before.
    """
    print("PHASE 2 — SQL migrations")
    targets = sorted({db for _, dbs in MIGRATIONS for db in dbs if db in live_dbs})
    for db in targets:
        res = psql(db, sql=LEDGER_DDL)
        if res.returncode != 0:
            print(f"  FAIL  ledger on {db}: {res.stderr.strip()[:120]}")
            failures.append(f"ledger {db}")

    for filename, dbs in MIGRATIONS:
        path = REPO / "migrations" / filename
        if not path.exists():
            print(f"  FAIL  {filename}: file missing")
            failures.append(f"missing {filename}")
            continue
        for db in dbs:
            if db not in live_dbs:
                print(f"  SKIP  {filename:<42} -> {db} (service not running)")
                continue
            res = psql(db, file=path)
            if res.returncode != 0:
                err = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["unknown"]
                print(f"  FAIL  {filename:<42} -> {db}: {err[0][:100]}")
                failures.append(f"{filename} on {db}")
                continue
            psql(db, sql="INSERT INTO schema_migrations (filename) VALUES "
                         f"('{filename}') ON CONFLICT (filename) DO NOTHING;")
            print(f"  OK    {filename:<42} -> {db}")
    print()


def verify(live_dbs):
    """Assert the things the platform actually breaks without.

    Only for databases whose owning service actually ran its model DDL. A check
    against a database nobody brought up proves nothing: order_saga_states is
    created by order-saga's models, so with order-saga absent its absence is
    the correct outcome, not a fault. Skipping is announced rather than silent,
    because a verification phase that quietly checks less than it appears to is
    how init_schemas.py used to report success over a broken schema.
    """
    print("PHASE 3 — verification")
    checks = [
        ("order_db", "SELECT 1 FROM information_schema.columns "
                     "WHERE table_name='outbox_messages' AND column_name='processed_at'",
         "outbox_messages.processed_at"),
        ("order_db", "SELECT 1 FROM information_schema.columns "
                     "WHERE table_name='outbox_messages' AND column_name='claimed_at'",
         "outbox_messages.claimed_at"),
        ("order_db", "SELECT 1 FROM information_schema.tables "
                     "WHERE table_name='processed_events'", "processed_events"),
        ("order_db", "SELECT 1 FROM information_schema.tables "
                     "WHERE table_name='order_saga_states'", "order_saga_states"),
        ("cart_db", "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name='cart_state'", "cart_state"),
        ("promo_db", "SELECT 1 FROM information_schema.tables "
                     "WHERE table_name='promotions'", "promotions"),
        ("payment_ledger_db", "SELECT 1 FROM information_schema.columns "
                              "WHERE table_name='idempotency_keys' AND column_name='result_id'",
         "idempotency_keys.result_id"),
        ("inventory_db", "SELECT 1 FROM information_schema.tables "
                         "WHERE table_name='inventory_items'", "inventory_items"),
    ]
    owner = {db: service for service, db in SERVICES.items()}
    for db, sql, label in checks:
        if db not in live_dbs:
            print(f"  SKIP  {db}.{label} ({owner.get(db, 'owner')} not running)")
            continue
        res = run(PSQL + ["-d", db, "-t", "-A", "-c", sql])
        if res.stdout.strip() == "1":
            print(f"  OK    {db}.{label}")
        else:
            print(f"  FAIL  {db}.{label} MISSING")
            failures.append(f"verify {db}.{label}")
    print()


if __name__ == "__main__":
    print("=" * 62)
    print(" SCHEMA BOOTSTRAP")
    print("=" * 62 + "\n")
    wait_for_postgres()
    phase_databases()
    live_dbs = phase_models()
    phase_migrations(live_dbs)
    verify(live_dbs)

    if failures:
        print(f"BOOTSTRAP FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("BOOTSTRAP COMPLETE — all schemas present and verified")
