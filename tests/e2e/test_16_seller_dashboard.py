"""
What a seller can see, and what a seller cannot do.

`bff-seller` exists for one reason: sellers must never reach internal services
directly (Rule 7). `order-saga` has an endpoint that marks a seller order
delivered — correct for the courier integration to call, catastrophic for a
seller. This is the file that proves the line is actually drawn.

Two boundaries, and both are the kind that look fine until somebody looks.

**Identity comes from the verified token and nowhere else.** A BFF that
accepts `?seller_id=` is a BFF where every seller reads every other seller's
orders, balance and customer addresses by changing one number, and it looks
entirely normal in a log. So the id is taken from a header the gateway sets
from the token, the gateway strips whatever the caller sent first, and the BFF
refuses a request that names a seller at all rather than quietly ignoring it.

**A seller may not attest to what only somebody else witnessed.** `deliver`
consumes stock and books escrow, so a seller who could mark their own parcel
delivered could trigger their own payout for goods still on their shelf.
`settle`, `mark_rto` and `complete_return` are the same shape.

The forgery checks go through the api-gateway rather than straight at the BFF,
because the gateway is where the header is set and stripping is only provable
from outside it.

Not in CI: it needs the gateway, bff-seller, seller-service, order-saga,
inventory-service, payment-service and catalog-service.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import asyncpg
import httpx

from config import db_url, describe, service_url

GATEWAY = service_url("api-gateway")
BFF = service_url("bff-seller") + "/api/seller"
SELLER_URL = service_url("seller-service") + "/sellers"
SAGA_URL = service_url("order-saga") + "/orders"

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def idem():
    return {"Idempotency-Key": str(uuid.uuid4())}


_connections = {}


async def db(database):
    if database not in _connections:
        _connections[database] = await asyncpg.connect(db_url(database))
    return _connections[database]


async def close_connections():
    for connection in _connections.values():
        await connection.close()
    _connections.clear()


def make_token(claims):
    """A JWT the gateway will accept, signed with the same secret it verifies.

    Minted here rather than obtained from a login flow because the property
    under test is what the *gateway* does with the claims, and a real login
    would only add a service that could fail for unrelated reasons.
    """
    secret = os.getenv("JWT_SECRET")
    if not secret:
        return None

    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({**claims, "exp": int(time.time()) + 3600}).encode())
    signature = b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(),
                             hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


async def onboard_active_seller(client, name):
    res = await client.post(SELLER_URL, json={
        "legal_name": f"{name} Ltd", "display_name": name,
        "contact_email": "dash@example.com"}, headers=idem(), timeout=60.0)
    if res.status_code != 201:
        return None
    seller_id = res.json()["id"]
    await client.post(f"{SELLER_URL}/{seller_id}/documents", json={
        "documents": [{"document_type": d, "media_id": str(uuid.uuid4())}
                      for d in ("national_id", "trade_licence")]}, timeout=60.0)
    await client.post(f"{SELLER_URL}/{seller_id}/review", timeout=60.0)
    await client.post(f"{SELLER_URL}/{seller_id}/review/result",
                      json={"approved": True}, timeout=60.0)
    contract = await client.get(service_url("seller-service") + "/contract",
                                timeout=60.0)
    await client.post(f"{SELLER_URL}/{seller_id}/contract", json={
        "version": contract.json()["current_contract_version"]}, timeout=60.0)
    return seller_id


async def place_order(client, seller_id):
    """A reserved seller order belonging to this seller."""
    import asyncio
    product_id = str(uuid.uuid4())
    conn = await db("inventory_db")
    await conn.execute(
        """INSERT INTO inventory_items
               (id, product_id, quantity_available, quantity_reserved, updated_at)
           VALUES ($1, $2, 20, 0, NOW())
           ON CONFLICT (product_id) DO UPDATE
               SET quantity_available = 20, quantity_reserved = 0""",
        uuid.uuid4(), uuid.UUID(product_id))

    res = await client.post(SAGA_URL, json={
        "user_id": str(uuid.uuid4()), "total_cents": 1500,
        "items_payload": [{"product_id": product_id, "seller_id": seller_id,
                           "quantity": 1, "price_cents": 1500,
                           "line_total_cents": 1500}],
    }, headers=idem(), timeout=60.0)
    if res.status_code != 201:
        return None, None
    order_id = res.json()["id"]
    for _ in range(30):
        await asyncio.sleep(1)
        split = await client.get(f"{SAGA_URL}/{order_id}/seller-orders",
                                 timeout=60.0)
        candidate = split.json()["seller_orders"][0]
        if candidate["status"] != "PENDING":
            return order_id, candidate["id"]
    return order_id, None


async def main():
    print("=" * 62)
    print(" SELLER DASHBOARD")
    print("=" * 62)
    print(f"-> {describe()}")

    async with httpx.AsyncClient() as client:

        print("\n1. Two sellers, one order each...")
        alpha = await onboard_active_seller(client, f"Alpha {uuid.uuid4().hex[:4]}")
        beta = await onboard_active_seller(client, f"Beta {uuid.uuid4().hex[:4]}")
        check(alpha and beta, "both active", "could not onboard two sellers")
        if not (alpha and beta):
            return finish()

        _, order_alpha = await place_order(client, alpha)
        order_beta_id, order_beta = await place_order(client, beta)
        check(order_alpha and order_beta, "both have a reserved order",
              "could not place an order for each seller")
        if not (order_alpha and order_beta):
            return finish()

        as_alpha = {"x-seller-id": alpha}

        print("\n2. Identity comes from the token, not the request...")
        res = await client.get(f"{BFF}/me", timeout=60.0)
        check(res.status_code == 401,
              "no identity is 401",
              f"an unidentified request returned {res.status_code}")

        res = await client.get(f"{BFF}/orders?seller_id={beta}",
                               headers=as_alpha, timeout=60.0)
        check(res.status_code == 400 and "may not be named" in res.text,
              "naming a seller in the query is refused, not ignored",
              f"?seller_id= returned {res.status_code}; a caller who believes "
              f"they scoped a request and gets an unscoped answer has been "
              f"misled")

        print("\n3. A seller sees only their own...")
        res = await client.get(f"{BFF}/orders", headers=as_alpha, timeout=60.0)
        queue = res.json()
        ids = [o["id"] for o in queue.get("seller_orders", [])]
        check(res.status_code == 200 and order_alpha in ids,
              f"alpha's queue has {queue.get('count')} order(s)",
              f"alpha's own order is missing from their queue: {res.status_code}")
        check(order_beta not in ids,
              "beta's order is not in alpha's queue",
              "one seller's queue contains another seller's order")

        res = await client.get(f"{BFF}/orders/{order_beta}", headers=as_alpha,
                               timeout=60.0)
        check(res.status_code == 404,
              "another seller's order is 404, not 403",
              f"reading another seller's order returned {res.status_code}; a "
              f"403 would confirm the id exists and let a seller enumerate")

        res = await client.get(f"{BFF}/orders/{order_alpha}", headers=as_alpha,
                               timeout=60.0)
        check(res.status_code == 200,
              "a seller can read their own order",
              f"a seller could not read their own order: {res.status_code}")

        print("\n4. The payout control...")
        for action in ("deliver", "settle", "mark_rto", "complete_return"):
            res = await client.post(f"{BFF}/orders/{order_alpha}/{action}",
                                    json={}, headers=as_alpha, timeout=60.0)
            check(res.status_code == 403,
                  f"{action} refused (403)",
                  f"a seller performed {action} on their own order "
                  f"({res.status_code}). Delivery consumes stock and books "
                  f"escrow; a seller claiming it triggers their own payout.")
            if res.status_code == 403:
                check(bool(res.json().get("detail")),
                      f"  and says why",
                      f"{action} was refused with no explanation")

        print("\n5. What a seller may do...")
        for action, body in (("confirm", {}),
                             ("dispatch", {"courier_name": "manual"})):
            res = await client.post(f"{BFF}/orders/{order_alpha}/{action}",
                                    json=body, headers=as_alpha, timeout=60.0)
            check(res.status_code == 200,
                  f"{action} -> {res.json().get('status')}",
                  f"{action} failed: {res.status_code} {res.text[:200]}")

        print("\n6. Acting on someone else's order...")
        res = await client.post(f"{BFF}/orders/{order_beta}/confirm", json={},
                                headers=as_alpha, timeout=60.0)
        check(res.status_code == 404,
              "alpha cannot confirm beta's order",
              f"alpha acted on beta's order: {res.status_code}")

        split = await client.get(f"{SAGA_URL}/{order_beta_id}/seller-orders",
                                 timeout=60.0)
        beta_status = split.json()["seller_orders"][0]["status"]
        check(beta_status == "INVENTORY_RESERVED",
              "beta's order is untouched",
              f"beta's order moved to {beta_status}; ownership is checked "
              f"before the action, not after")

        print("\n7. Money and catalogue are scoped...")
        res = await client.get(f"{BFF}/balance", headers=as_alpha, timeout=60.0)
        check(res.status_code == 200 and res.json()["seller_id"] == alpha,
              f"balance is alpha's: {res.json().get('owed_cents')}",
              f"the balance endpoint answered for "
              f"{res.json().get('seller_id')}")

        print("\n8. A forged header through the gateway...")
        # The gateway is where the header is set, so stripping is only
        # provable from outside it.
        token = make_token({"sub": "buyer-1"})
        if token is None:
            print("      JWT_SECRET not set; skipping the gateway checks")
        else:
            res = await client.get(f"{GATEWAY}/api/seller/me", timeout=60.0)
            check(res.status_code == 401, "no token is 401 at the gateway",
                  f"an unauthenticated request reached the BFF: "
                  f"{res.status_code}")

            res = await client.get(
                f"{GATEWAY}/api/seller/me",
                headers={"Authorization": f"Bearer {token}",
                         "x-seller-id": beta}, timeout=60.0)
            check(res.status_code == 401,
                  "a buyer's token plus a forged header is still 401",
                  f"a forged x-seller-id survived the gateway on a token with "
                  f"no seller claim: {res.status_code}. Every logged-in "
                  f"customer would have a seller dashboard.")

            seller_token = make_token({"sub": "user-1", "seller_id": alpha})
            res = await client.get(
                f"{GATEWAY}/api/seller/me",
                headers={"Authorization": f"Bearer {seller_token}",
                         "x-seller-id": beta}, timeout=60.0)
            check(res.status_code == 200 and res.json().get("id") == alpha,
                  "a seller's token wins over a forged header",
                  f"a forged header changed the identity to "
                  f"{res.json().get('id')}; it must be alpha")

    await close_connections()
    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] A seller sees only their own orders, cannot name another "
          "seller, cannot forge one through the gateway, and cannot mark "
          "their own parcel delivered.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
