#!/usr/bin/env python3
"""
Run the end-to-end suite against whichever target is configured.

    python tests/e2e/run_suite.py                    # compose (default)
    E2E_TARGET=forward PG_PORT=15432 PG_PASSWORD=... python tests/e2e/run_suite.py

Order matters: test_02 and test_03 read the product id test_01 writes to
.e2e_state.json, so the suite is sequential by design rather than by accident.

Exits non-zero if any test fails. Prints the target first, because a green run
against the wrong environment is worse than a red one.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from config import describe  # noqa: E402

TESTS = [
    "test_01_catalog_write_to_read_sync",
    "test_02_cqrs_distributed_updates",
    "test_03_saga_orchestrator",
    "test_04_full_checkout_flow",
    "test_05_auxiliary_services",
    "test_06_intra_mesh_connectivity",
    "test_07_cart_cache_coherence",
    "test_08_rate_limit_state_loss",
    # Before the two that stop infrastructure: it needs seller-service and
    # media-service answering, and nothing it does disturbs either.
    "test_11_seller_onboarding",
    # After test_11: it onboards its own sellers, but it needs the same
    # services answering and there is no reason to interleave them.
    "test_12_order_splitting",
    # Seeds and consumes its own stock, so it neither depends on nor
    # disturbs the inventory the earlier tests arranged.
    "test_13_cod_lifecycle",
    # After test_13: it drives the same lifecycle from a courier callback
    # rather than from the API, and seeds its own stock either way.
    "test_14_courier_and_settlement",
    # After test_14: it books the ledger from a delivery driven through
    # the API, and onboards its own sellers so it shares no fixtures.
    "test_15_escrow_ledger",
    # Needs JWT_SECRET in the environment for the gateway checks; it
    # skips those and still asserts the rest without one.
    "test_16_seller_dashboard",
    # Drives sixteen order lifecycles to build two real fulfilment
    # records, so it is the slowest in the suite by some way.
    "test_17_ranking_and_metrics",
    "test_18_reviews",
    "test_19_seller_projection",
    "test_20_personalisation",
    "test_21_seller_suspension",
    "test_22_review_moderation",
    # Last on purpose: these two stop infrastructure. Both put it back and
    # wait for recovery before finishing, but running either earlier would
    # have every test after it sharing a cluster mid-repair.
    "test_09_broker_loss",
    # Leaves the promoted Redis node as primary, which is a valid steady
    # state -- sentinel does not fail back. Everything that reads Redis goes
    # through the sentinels, so nothing downstream cares which node it is.
    "test_10_redis_failover",
]


def main() -> int:
    print("=" * 62)
    print(f" E2E SUITE  |  {describe()}")
    print("=" * 62)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    failed = []

    for name in TESTS:
        proc = subprocess.run([sys.executable, f"{name}.py"], cwd=HERE,
                              capture_output=True, text=True, env=env)
        if proc.returncode == 0:
            print(f"  PASS  {name}")
        else:
            failed.append(name)
            print(f"  FAIL  {name}  (exit {proc.returncode})")
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
            for line in tail:
                print(f"        {line}")

    print("-" * 62)
    print(f"  {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
