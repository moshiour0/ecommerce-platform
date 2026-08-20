"""
What the rate limiter does when its state disappears.

The gateway's budgets live in Redis and nowhere else. A cart survives losing
its Redis copy because Postgres still has it -- that is what test_07 proves --
but a rate limit has no durable counterpart. Whatever is lost is gone, and the
caller gets a fresh allowance.

That is a real property of the design rather than a bug to fix, and it is worth
a test precisely because it is invisible: nothing errors, nothing logs, and a
budget that quietly reset looks exactly like a budget that was never spent. It
is characterised here so it is known in advance rather than discovered during
the incident it makes worse.

The part that IS a guard is the last assertion. Amnesty after a restart is
tolerable: it costs one window, it needs someone to restart Redis, and it
affects everyone equally. Eviction is not. If Redis were configured with a
maxmemory limit and an eviction policy that can discard arbitrary keys, a
caller could push the instance into memory pressure -- filling carts is enough
-- and have their own rate-limit counters evicted. That turns a shared
operational weakness into a per-attacker bypass, and it is exactly the shape of
thing that gets configured by accident later.
"""

import asyncio
import subprocess
import sys

import httpx

from config import describe, mesh_exec_prefix, redis_primary_service, service_url

GATEWAY = service_url("api-gateway")
# Unrouted on purpose: it stops at the gateway's own 404 handler, so exhausting
# a budget puts no load on any BFF. The limiter runs before the fallback.
PROBE = "/rl-probe"
READ_BUDGET = 200

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def redis_cli(*args):
    """redis-cli inside the mesh. docker exec or kubectl exec, per target."""
    result = subprocess.run(mesh_exec_prefix(redis_primary_service()) + ["redis-cli", *args],
                            capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        print(f"      [!] redis-cli {' '.join(args)}: {result.stderr.strip()}")
        return ""
    return result.stdout.strip()


async def main():
    print("=" * 62)
    print(" RATE LIMITER UNDER STATE LOSS")
    print("=" * 62)
    print(f"-> {describe()}")

    async with httpx.AsyncClient(timeout=15.0) as client:

        print("\n1. Spending the read budget...")
        # Generously more than the budget: this source may have spent some of
        # it already, and the assertion is about being refused, not about the
        # exact count.
        codes = []
        for _ in range(READ_BUDGET + 40):
            codes.append((await client.get(f"{GATEWAY}{PROBE}")).status_code)

        refused = codes.count(429)
        unexpected = [c for c in codes if c not in (404, 429)]
        check(refused > 0, f"budget enforced ({refused} refused)",
              "the limiter never refused a request; it is not enforcing at all")
        check(not unexpected, "no unexpected statuses",
              f"gateway returned {sorted(set(unexpected))} under load")

        print("\n2. Confirming the limiter is refusing this caller...")
        before = (await client.get(f"{GATEWAY}{PROBE}")).status_code
        check(before == 429, "caller is over budget",
              f"expected 429 while over budget, got {before}")

        print("\n3. Locating the counters in Redis...")
        keys = [k for k in redis_cli("--scan", "--pattern", "rl:*").splitlines() if k]
        check(bool(keys), f"{len(keys)} limiter key(s) present",
              "no rl:* keys in Redis; the limiter is not using the shared store")

        print("\n4. Losing them, as a restart or a flush would...")
        for key in keys:
            redis_cli("DEL", key)

        after = (await client.get(f"{GATEWAY}{PROBE}")).status_code
        # Characterisation, not a wish: with the counters gone the caller is
        # allowed again immediately. Asserted so the day it changes, someone
        # notices and updates this comment rather than being surprised.
        check(after == 404,
              "budget resets when its state is lost, as designed",
              f"expected the caller to be admitted after state loss, got {after}. "
              f"If the limiter now survives a flush, this test is out of date "
              f"and the behaviour improved.")

    print("\n5. Checking those counters cannot be evicted under memory pressure...")
    # This is the assertion that matters. Amnesty after a restart costs one
    # window and hits everyone equally. Eviction under pressure is a per-caller
    # bypass: fill Redis, lose your own counters, keep going.
    policy = redis_cli("CONFIG", "GET", "maxmemory-policy").splitlines()[-1:] or [""]
    maxmemory = redis_cli("CONFIG", "GET", "maxmemory").splitlines()[-1:] or [""]
    policy, maxmemory = policy[0].strip(), maxmemory[0].strip()
    print(f"      maxmemory={maxmemory}  maxmemory-policy={policy}")

    unlimited = maxmemory in ("0", "")
    check(unlimited or policy == "noeviction",
          f"limiter counters are not evictable (maxmemory={maxmemory}, "
          f"policy={policy})",
          f"Redis may evict arbitrary keys (maxmemory={maxmemory}, "
          f"policy={policy}). A caller can push the instance into memory "
          f"pressure and have their own rate-limit counters discarded, which "
          f"turns a shared weakness into a per-attacker bypass.")

    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("[SUCCESS] The limiter enforces, loses state as designed, and its "
          "counters cannot be evicted.")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
