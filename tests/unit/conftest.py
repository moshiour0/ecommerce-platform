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
