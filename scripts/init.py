#!/usr/bin/env python3
"""
Bring the whole platform up from nothing, and prove it works.

    python scripts/init.py            # bring up + bootstrap + connectors
    python scripts/init.py --verify   # also run the e2e suite
    python scripts/init.py --fresh    # destroy volumes first (asks first)

`make` is not installed on every machine this repo runs on (it is absent on
the primary Windows development box), so the Makefile alone is documentation
rather than something you can execute. This script is the portable equivalent
and is what CI should call.

Ordering is not incidental:
  1. compose up      -- health-gated, so nothing starts before its dependency
                        is actually ready
  2. schema          -- model DDL, then SQL migrations, then assertions
  3. CDC connectors  -- last, because Debezium can only capture tables that
                        already exist

Exits non-zero at the first failure. A platform that came up broken must not
report success.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.apps.yml"]
INFRA = ["postgres", "redis", "zookeeper", "kafka", "schema-registry", "elasticsearch", "debezium"]


def sh(cmd, capture=True, check=False):
    res = subprocess.run(cmd, cwd=REPO, capture_output=capture, text=True)
    if check and res.returncode != 0:
        sys.exit(f"FATAL: {' '.join(cmd)}\n{res.stderr or res.stdout}")
    return res


def step(msg):
    print(f"\n{'=' * 62}\n {msg}\n{'=' * 62}")


def ensure_env():
    env, example = REPO / ".env", REPO / ".env.example"
    if env.exists():
        print("  .env exists — leaving it alone")
        return
    # Never regenerate over an existing .env: it holds the JWT_SECRET, and
    # replacing it invalidates every token already issued.
    import secrets
    text = example.read_text(encoding="utf-8")
    secret = secrets.token_hex(32)
    if "JWT_SECRET=" in text:
        text = "\n".join(
            f"JWT_SECRET={secret}" if l.startswith("JWT_SECRET=") else l
            for l in text.splitlines()
        ) + "\n"
    else:
        text += f"\nJWT_SECRET={secret}\n"
    env.write_text(text, encoding="utf-8")
    print("  .env created with a generated JWT_SECRET")


def wait_healthy(timeout=300):
    print("  waiting for infrastructure health gates...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = []
        for svc in INFRA:
            out = sh(["docker", "inspect", "-f",
                      "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                      f"ecommerce-platform-{svc}-1"]).stdout.strip()
            if out not in ("healthy", "none"):
                pending.append(f"{svc}={out or 'missing'}")
        if not pending:
            print("  all infrastructure healthy")
            return
        print(f"    still waiting: {', '.join(pending)}")
        time.sleep(10)
    sys.exit("FATAL: infrastructure did not become healthy in time")


def census():
    running = sh(["docker", "ps", "-q", "--filter", "name=ecommerce-platform"]).stdout.split()
    exited = sh(["docker", "ps", "-aq", "--filter", "name=ecommerce-platform",
                 "--filter", "status=exited"]).stdout.split()
    print(f"  running: {len(running)}   exited: {len(exited)}")
    if exited:
        names = sh(["docker", "ps", "-a", "--filter", "name=ecommerce-platform",
                    "--filter", "status=exited", "--format", "{{.Names}}"]).stdout.strip()
        sys.exit(f"FATAL: containers exited during bring-up:\n{names}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true",
                    help="destroy all volumes first (DESTRUCTIVE, prompts)")
    ap.add_argument("--verify", action="store_true", help="run the e2e suite at the end")
    args = ap.parse_args()

    if args.fresh:
        print("This deletes ALL database volumes. Order, payment and catalog")
        print("data will be lost and is not recoverable.")
        if input("Type 'yes' to continue: ").strip() != "yes":
            sys.exit("Aborted.")
        step("DESTROYING VOLUMES")
        sh(COMPOSE + ["down", "-v"], capture=False)

    step("ENVIRONMENT")
    ensure_env()

    step("BRINGING UP CONTAINERS")
    sh(COMPOSE + ["up", "-d"], capture=False, check=True)
    wait_healthy()
    census()

    step("SCHEMA BOOTSTRAP")
    if sh([sys.executable, "scripts/bootstrap_schema.py"], capture=False).returncode != 0:
        sys.exit("FATAL: schema bootstrap failed")

    step("CDC CONNECTORS")
    if sh([sys.executable, "fix_connectors.py"], capture=False).returncode != 0:
        sys.exit("FATAL: connector provisioning failed")
    time.sleep(20)
    import json
    names = json.loads(sh(["curl", "-s", "-m", "10", "http://localhost:8083/connectors"]).stdout)
    bad = []
    for c in sorted(names):
        raw = sh(["curl", "-s", "-m", "10", f"http://localhost:8083/connectors/{c}/status"]).stdout
        states = [t["state"] for t in json.loads(raw).get("tasks", [])]
        if states != ["RUNNING"]:
            bad.append(f"{c}={states or 'NO_TASKS'}")
    print(f"  connectors RUNNING: {len(names) - len(bad)}/{len(names)}")
    if bad:
        sys.exit("FATAL: connectors not running:\n  " + "\n  ".join(bad))

    if args.verify:
        step("E2E SUITE")
        tests = ["test_01_catalog_write_to_read_sync", "test_02_cqrs_distributed_updates",
                 "test_03_saga_orchestrator", "test_04_full_checkout_flow",
                 "test_05_auxiliary_services", "test_06_intra_mesh_connectivity"]
        import os
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        failed = []
        for t in tests:
            rc = subprocess.run([sys.executable, f"{t}.py"], cwd=REPO / "tests" / "e2e",
                                capture_output=True, text=True, env=env).returncode
            print(f"  {'PASS' if rc == 0 else 'FAIL'}  {t}")
            if rc != 0:
                failed.append(t)
        if failed:
            sys.exit(f"FATAL: {len(failed)} test(s) failed")

    print("\n" + "=" * 62)
    print(" PLATFORM READY")
    print("=" * 62)


if __name__ == "__main__":
    main()
