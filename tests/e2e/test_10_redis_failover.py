"""
What the platform does when the Redis primary dies.

A replica and three sentinels are a configuration. The configuration is
satisfied by a topology that still takes a full outage on a primary failure --
all it needs is one client connecting to `redis` by hostname instead of
through the sentinels, and that client keeps talking to the demoted node
forever. Nothing reports it: the container is up, the connection is open, and
the commands succeed against a node that is no longer the primary.

So this kills the primary for real and asks four things:

  1. Do the sentinels agree and promote? Within their down-after window, the
     replica must become the primary and all three sentinels must report the
     new address.

  2. Does the cart survive? A cart written before the failure must be readable
     after it, through the API, with no manual intervention.

  3. Do writes resume? Adding to a cart after the promotion must work. This is
     the assertion that fails if a client is pinned to a hostname: the write
     goes to the demoted node, which is now a read-only replica, and comes
     back with READONLY.

  4. Does the old primary rejoin as a replica? When it comes back it must be
     reconfigured by the sentinels rather than resuming as a second primary --
     two writable Redises taking different data is worse than the outage.

Not in CI. It stops infrastructure by container name, so it only runs against
compose, and it needs cart-service plus the whole Redis topology.
"""

import subprocess
import sys
import time
import uuid

import httpx

from config import describe, service_url

CART_URL = service_url("cart-service") + "/cart"

# Which container is the primary is discovered, not assumed. Sentinel does not
# fail back, so after one run of this test the promoted node stays primary --
# and a version of this file that killed `ecommerce-platform-redis-1` every
# time would pass once and then kill a replica and report a broken failover.
NODES = ["ecommerce-platform-redis-1", "ecommerce-platform-redis-replica-1"]
SENTINELS = [f"ecommerce-platform-redis-sentinel-{n}-1" for n in (1, 2, 3)]
MASTER_NAME = "mymaster"

# down-after-milliseconds is 5s and failover-timeout is 10s, so a promotion
# should be agreed well inside this. Generous because the assertion is that it
# happens at all, not that it happens quickly.
FAILOVER_BUDGET_SECONDS = 60

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def sentinel_primary(container):
    """Which host this sentinel currently calls the primary."""
    result = subprocess.run(
        ["docker", "exec", "-i", container, "redis-cli", "-p", "26379",
         "sentinel", "get-master-addr-by-name", MASTER_NAME],
        capture_output=True, text=True, timeout=30)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else ""


def container_ip(container):
    result = subprocess.run(
        ["docker", "inspect", "-f",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
        capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def identify_nodes():
    """(primary_container, replica_container, primary_address) from sentinel."""
    address = sentinel_primary(SENTINELS[0])
    for name in NODES:
        if container_ip(name) and container_ip(name) == address:
            other = [n for n in NODES if n != name]
            return name, other[0], address
    return NODES[0], NODES[1], address


def role_of(container):
    result = subprocess.run(
        ["docker", "exec", "-i", container, "redis-cli", "info", "replication"],
        capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        if line.startswith("role:"):
            return line.split(":", 1)[1].strip()
    return "unreachable"


def wait_for_promotion(old_primary_host, budget=FAILOVER_BUDGET_SECONDS):
    """Block until a quorum of sentinels names something else as primary."""
    deadline = time.time() + budget
    while time.time() < deadline:
        answers = [sentinel_primary(s) for s in SENTINELS]
        promoted = [a for a in answers if a and a != old_primary_host]
        if len(promoted) >= 2:          # the quorum
            return promoted[0], answers
        time.sleep(2)
    return "", [sentinel_primary(s) for s in SENTINELS]


def main():
    print("=" * 62)
    print(" REDIS PRIMARY FAILOVER")
    print("=" * 62)
    print(f"-> {describe()}")

    user_id = str(uuid.uuid4())
    product_id = str(uuid.uuid4())

    with httpx.Client(timeout=30.0) as client:

        print("\n1. Confirming the sentinels agree on the current primary...")
        before = [sentinel_primary(s) for s in SENTINELS]
        print(f"      sentinels report: {before}")
        check(len(set(before)) == 1 and before[0],
              f"all three sentinels name {before[0]!r}",
              f"sentinels disagree about the primary: {before}. A failover "
              f"cannot be agreed from here.")
        primary_container, replica_container, original = identify_nodes()
        print(f"      primary is {primary_container} at {original}")
        print(f"      replica is {replica_container}")

        print("\n2. Writing a cart through the API...")
        response = client.post(
            f"{CART_URL}/{user_id}/items",
            json={"item": {"product_id": product_id, "quantity": 2}},
            headers={"Idempotency-Key": str(uuid.uuid4())})
        check(response.status_code in (200, 201),
              f"cart written (HTTP {response.status_code})",
              f"could not write a cart before the failover: "
              f"HTTP {response.status_code} {response.text[:200]}")

        print(f"\n3. Killing the primary ({original})...")
        # SIGKILL, not a graceful stop. A clean shutdown lets Redis save and
        # close connections tidily, which is the easy case; a kill is what a
        # host failure looks like.
        subprocess.run(["docker", "kill", primary_container],
                       capture_output=True, timeout=60)

        print("\n4. Waiting for the sentinels to promote the replica...")
        started = time.time()
        promoted, answers = wait_for_promotion(original)
        elapsed = time.time() - started
        check(bool(promoted),
              f"promoted to {promoted!r} in ~{elapsed:.0f}s",
              f"no promotion within {FAILOVER_BUDGET_SECONDS}s; sentinels "
              f"still report {answers}. The replica is decoration.")

        if promoted:
            check(role_of(replica_container) == "master",
                  f"{replica_container} now reports role=master",
                  f"{replica_container} reports role={role_of(replica_container)} "
                  f"while the sentinels have promoted it")

        print("\n5. Reading the cart back, through the API...")
        # Retried, because the client is reconnecting through sentinel while
        # this runs. Retrying is the correct behaviour to assert: the outage
        # should be seconds, not permanent.
        body, status = None, 0
        for _ in range(15):
            try:
                response = client.get(f"{CART_URL}/{user_id}")
                status = response.status_code
                if status == 200:
                    body = response.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)

        items = (body or {}).get("items", [])
        check(status == 200,
              "cart readable after the primary was killed",
              f"cart unreadable after the failover (HTTP {status}). Postgres "
              f"still holds it, so this is the cache path failing rather than "
              f"data loss.")
        check(any(i.get("product_id") == product_id for i in items),
              "the item written before the failure is still there",
              f"the cart came back without the item: {items}")

        print("\n6. Writing again, now that the replica is the primary...")
        # The assertion that catches a client pinned to a hostname: the write
        # lands on the demoted node, which is read-only, and Redis answers
        # READONLY rather than failing the connection.
        wrote, detail = False, ""
        for _ in range(15):
            try:
                response = client.post(
                    f"{CART_URL}/{user_id}/items",
                    json={"item": {"product_id": str(uuid.uuid4()),
                                   "quantity": 1}},
                    headers={"Idempotency-Key": str(uuid.uuid4())})
                if response.status_code in (200, 201):
                    wrote = True
                    break
                detail = f"HTTP {response.status_code} {response.text[:200]}"
            except httpx.HTTPError as e:
                detail = str(e)
            time.sleep(2)

        check(wrote,
              "writes resumed against the promoted primary",
              f"writes did not resume: {detail}. A READONLY error here means a "
              f"client is connecting to a hostname rather than through the "
              f"sentinels, and is still talking to the demoted node.")

    print(f"\n7. Restarting the old primary ({primary_container})...")
    subprocess.run(["docker", "start", primary_container],
                   capture_output=True, timeout=120)

    print("\n8. Checking it rejoins as a replica rather than a second primary...")
    rejoined = ""
    for _ in range(20):
        time.sleep(3)
        rejoined = role_of(primary_container)
        if rejoined == "slave":
            break
    check(rejoined == "slave",
          f"{primary_container} came back as a replica of the promoted node",
          f"{primary_container} came back reporting role={rejoined!r}. Two primaries "
          f"accepting different writes is worse than the outage that caused "
          f"it.")

    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        print("\nNOTE: the topology is left with the replica as primary. That "
              "is a valid steady state -- sentinel does not fail back -- but "
              "`python scripts/check_redis.py` points at the original names "
              "and will report the roles swapped until the next bring-up.")
        return 1
    print("[SUCCESS] The primary was killed, the sentinels promoted the "
          "replica, carts kept serving, writes resumed, and the old primary "
          "rejoined as a replica.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
