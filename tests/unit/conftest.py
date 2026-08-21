"""
Module loading for unit tests.

Every service in this repository has a top-level package called `app`, so
putting two service roots on sys.path makes Python's module cache serve
whichever imported first -- `app.dispatch_rules` then fails to resolve because
`app` already means order-saga.

Loading each module directly from its file under a unique name sidesteps the
collision entirely, and keeps the tests independent of how services are
packaged. Both modules under test are deliberately dependency-free (no
relative imports, no I/O at import time), which is what makes this possible.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def load_module(unique_name: str, relative_path: str):
    """Import a single .py file under a name that cannot collide."""
    path = REPO / relative_path
    if not path.exists():
        raise FileNotFoundError(f"module under test not found: {path}")
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


saga_transitions = load_module(
    "saga_transitions_under_test",
    "services/order-saga/app/services/transitions.py",
)

dispatch_rules = load_module(
    "dispatch_rules_under_test",
    "workers/saga-dispatcher/app/dispatch_rules.py",
)

refund_rules = load_module(
    "refund_rules_under_test",
    "services/payment-service/app/services/refund_rules.py",
)

charge_rules = load_module(
    "charge_rules_under_test",
    "services/payment-service/app/services/charge_rules.py",
)

checkout_lock = load_module(
    "checkout_lock_under_test",
    "services/cart-service/app/services/checkout_lock.py",
)

reservation_rules = load_module(
    "reservation_rules_under_test",
    "services/inventory-service/app/services/reservation_rules.py",
)

cart_cache_rules = load_module(
    "cart_cache_rules_under_test",
    "services/cart-service/app/services/cart_cache_rules.py",
)

media_rules = load_module(
    "media_rules_under_test",
    "services/media-service/app/services/media_rules.py",
)

audit_rules = load_module(
    "audit_rules_under_test",
    "services/audit-service/app/services/audit_rules.py",
)

webhook_rules = load_module(
    "webhook_rules_under_test",
    "workers/webhook-handler/app/webhook_rules.py",
)

notification_rules = load_module(
    "notification_rules_under_test",
    "workers/notification-worker/app/notification_rules.py",
)

dlq_rules = load_module(
    "dlq_rules_under_test",
    "workers/dlq-reprocessor/app/dlq_rules.py",
)

reindex_rules = load_module(
    "reindex_rules_under_test",
    "workers/reindex-worker/app/reindex_rules.py",
)

catalog_rules = load_module(
    "catalog_rules_under_test",
    "services/catalog-service/app/services/catalog_rules.py",
)

search_rules = load_module(
    "search_rules_under_test",
    "services/search-service/app/services/search_rules.py",
)

# The shared library, loaded from its file like everything else so the unit
# tier still needs nothing on PYTHONPATH.
read_model = load_module(
    "read_model_under_test",
    "shared/libs/python-common/read_model.py",
)

event_rules = load_module(
    "event_rules_under_test",
    "workers/stream-processor/app/consumers/event_rules.py",
)

# fix_connectors.py imports requests lazily, inside the functions that talk to
# Debezium, so the config builder can be loaded here with nothing installed.
connector_config = load_module(
    "connector_config_under_test",
    "fix_connectors.py",
)

kafka_replication = load_module(
    "kafka_replication_under_test",
    "shared/libs/python-common/kafka_replication.py",
)

redis_topology = load_module(
    "redis_topology_under_test",
    "shared/libs/python-common/redis_topology.py",
)

seller_rules = load_module(
    "seller_rules_under_test",
    "services/seller-service/app/services/seller_rules.py",
)

split_rules = load_module(
    "split_rules_under_test",
    "services/order-saga/app/services/split_rules.py",
)
