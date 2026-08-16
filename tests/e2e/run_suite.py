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
