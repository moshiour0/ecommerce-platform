"""
Cart cache coherence: what happens when Redis loses a live cart.

Unit tests (tests/unit/test_cart_cache_rules.py) pin the decision against
hand-built inputs. They cannot prove that the service reads both stores, that a
recovered cart is written back, or that a cart which is genuinely gone stays
gone. This deletes the Redis key of a live cart out from under the running
service and asserts what the user sees afterwards.

The scenario is an eviction, and it needs no exotic setup to be realistic:
Redis is memory, so a maxmemory trim, a restart, or a failover produces exactly
this. Before the fallback existed the observed behaviour was

    GET /cart/{user}        -> 200 {"items": []}      (cart_state still active)
    add another product     -> cart_state overwritten, the first product gone

so a cache eviction permanently destroyed the durable copy on the next write.

The second half is the guard against over-correcting: a cart that has been
checked out must NOT come back when its cache entry disappears, or a lost key
turns into a second order for items already handed to the saga.
"""

import asyncio
import json
import subprocess
import sys
import uuid

import asyncpg
import httpx

from config import service_url, db_url, describe, mesh_exec_prefix

CART_URL = service_url("cart-service") + "/cart"
CART_DB_URL = db_url("cart_db")

failures = []


def check(condition, ok_message, fail_message):
    if condition:
        print(f"      [OK] {ok_message}")
    else:
        print(f"      [FAIL] {fail_message}")
        failures.append(fail_message)


def redis_cli(*args):
    """Run redis-cli inside the mesh. docker exec or kubectl exec, per target."""
    cmd = mesh_exec_prefix("redis") + ["redis-cli", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"      [!] redis-cli {' '.join(args)} failed: {result.stderr.strip()}")
        return ""
    return result.stdout.strip()


async def durable_cart(conn, user_id):
    """The cart_state row as (items, status), or (None, None)."""
    row = await conn.fetchrow(
        "SELECT items, status FROM cart_state WHERE user_id = $1 "
        "ORDER BY expires_at DESC LIMIT 1",
        uuid.UUID(user_id))
    if row is None:
        return None, None
    raw = row["items"]
    items = json.loads(raw) if isinstance(raw, str) else raw
    return items, row["status"]


def items_of(payload):
    """{product_id: quantity} from a CartResponse body."""
    return {i["product_id"]: i["quantity"] for i in payload["items"]}


async def main():
    print("==================================================")
    print(" CART CACHE COHERENCE UNDER EVICTION")
    print("==================================================")
    print(f"-> {describe()}")

    user_id = str(uuid.uuid4())
    product_a = str(uuid.uuid4())
    product_b = str(uuid.uuid4())
    cart_key = f"cart:{user_id}"
    print(f"-> user {user_id[:8]}  A={product_a[:8]}  B={product_b[:8]}")

    conn = await asyncpg.connect(CART_DB_URL)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:

            print("\n1. Seeding a cart with product A x2...")
            res = await client.post(
                f"{CART_URL}/{user_id}/items",
                json={"item": {"product_id": product_a, "quantity": 2}},
                headers={"Idempotency-Key": str(uuid.uuid4())})
            if res.status_code != 200:
                print(f"   [FAIL] could not seed cart: {res.status_code} {res.text[:200]}")
                sys.exit(1)
            check(items_of(res.json()) == {product_a: 2},
                  "cart holds A x2", f"unexpected cart after seed: {res.text[:200]}")

            items, status = await durable_cart(conn, user_id)
            check(items == {product_a: 2} and status == "active",
                  f"cart_state agrees: {items}, status={status}",
                  f"cart_state disagrees after seed: {items}, status={status}")

            print("\n2. Evicting the cart from Redis (key DELETE, nothing else)...")
            deleted = redis_cli("DEL", cart_key)
            check(deleted == "1", "redis key deleted",
                  f"expected to delete 1 key, redis-cli said {deleted!r}")

            print("\n3. Reading the cart with the cache gone...")
            res = await client.get(f"{CART_URL}/{user_id}")
            got = items_of(res.json()) if res.status_code == 200 else None
            check(got == {product_a: 2},
                  "cart survived the eviction, rehydrated from postgres",
                  f"cart came back as {got} after eviction -- the durable row "
                  f"still holds A x2, so the user was shown someone else's idea "
                  f"of an empty cart")

            print("\n4. Checking the recovered cart was written back to Redis...")
            exists = redis_cli("EXISTS", cart_key)
            ttl = redis_cli("TTL", cart_key)
            check(exists == "1", "redis re-warmed after the miss",
                  "redis was not re-warmed, so every read pays for postgres")
            # Re-warming must not extend the cart's life past the expires_at the
            # sweeper works from, so the TTL is the remainder, never a fresh hour.
            check(ttl.lstrip("-").isdigit() and 0 < int(ttl) <= 3600,
                  f"re-warm TTL is the cart's remaining life ({ttl}s)",
                  f"unexpected TTL after re-warm: {ttl!r}")

            print("\n5. Writing to the recovered cart (product B x1)...")
            res = await client.post(
                f"{CART_URL}/{user_id}/items",
                json={"item": {"product_id": product_b, "quantity": 1}},
                headers={"Idempotency-Key": str(uuid.uuid4())})
            got = items_of(res.json()) if res.status_code == 200 else None
            check(got == {product_a: 2, product_b: 1},
                  "both products present after the write",
                  f"write after eviction returned {got}; product A was dropped")

            items, status = await durable_cart(conn, user_id)
            check(items == {product_a: 2, product_b: 1},
                  f"cart_state preserved both products: {items}",
                  f"cart_state was overwritten to {items} -- a cache miss "
                  f"destroyed the durable copy")

            print("\n6. Checking out, then evicting again...")
            res = await client.post(
                f"{CART_URL}/{user_id}/checkout",
                headers={"Idempotency-Key": str(uuid.uuid4())})
            check(res.status_code == 202,
                  f"checkout accepted ({res.status_code})",
                  f"checkout failed: {res.status_code} {res.text[:200]}")

            items, status = await durable_cart(conn, user_id)
            check(status == "checkout_in_progress",
                  f"cart_state moved to {status}",
                  f"expected checkout_in_progress, got {status}")

            # Checkout already deletes the key; delete again so the state under
            # test is unambiguous regardless of that.
            redis_cli("DEL", cart_key)

            print("\n7. A checked-out cart must NOT come back...")
            res = await client.get(f"{CART_URL}/{user_id}")
            got = items_of(res.json()) if res.status_code == 200 else None
            check(got == {},
                  "checked-out cart stayed gone",
                  f"a checked-out cart was resurrected as {got}; those items are "
                  f"already inside a saga, so this is a duplicate order waiting "
                  f"to happen")

    finally:
        await conn.close()

    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("[SUCCESS] The cart survives eviction, and a dead cart stays dead.")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
