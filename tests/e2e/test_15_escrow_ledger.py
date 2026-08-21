"""
What the platform owes a seller, from the door to the payout.

The unit tests prove the arithmetic balances against fake entries. What they
cannot show is that delivering a real order actually books it: order-saga has
to emit the command, the dispatcher has to claim and execute it, payment-service
has to read the seller's accepted commission rate, and the numbers have to come
out the other end. Every one of those links has failed silently at least once
in this codebase's history.

The assertions worth naming:

**The liability appears at delivery, not at payout.** A balance computed at
payout time by summing orders answers "what do we think we owe" and cannot
answer "what did we owe last Tuesday" or "why does this disagree with the
orders". So the ledger must show the seller owed money the moment the courier
collects it.

**Booking twice does not pay twice.** Delivery reaches payment-service through
the outbox and the dispatcher, which is at-least-once, and courier callbacks
resend on top of that. A second booking would credit the seller again for one
parcel.

**Settlement does not change what the seller is owed.** That was decided at
delivery. A slow courier is the platform's problem, not a reason to owe the
seller less.

**The whole ledger balances.** Every entry ever written must sum to zero. A
non-zero total means a transaction was stored unbalanced and the books are
wrong by exactly that amount.

Not in CI: it needs seller-service, order-saga, inventory-service and
payment-service.
"""

import sys
import uuid

import asyncpg
import httpx

from config import db_url, describe, service_url

SAGA_URL = service_url("order-saga") + "/orders"
SELLER_URL = service_url("seller-service") + "/sellers"
PAYMENT = service_url("payment-service")

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


async def onboard_active_seller(client, name):
    """Only an active seller has an accepted contract, and so a rate."""
    res = await client.post(SELLER_URL, json={
        "legal_name": f"{name} Ltd", "display_name": name,
        "contact_email": "escrow@example.com"}, headers=idem(), timeout=60.0)
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


async def delivered_seller_order(client, seller_id, units, unit_price):
    """A seller order driven all the way to DELIVERED."""
    import asyncio
    product_id = str(uuid.uuid4())
    conn = await db("inventory_db")
    await conn.execute(
        """INSERT INTO inventory_items
               (id, product_id, quantity_available, quantity_reserved, updated_at)
           VALUES ($1, $2, 50, 0, NOW())
           ON CONFLICT (product_id) DO UPDATE
               SET quantity_available = 50, quantity_reserved = 0""",
        uuid.uuid4(), uuid.UUID(product_id))

    res = await client.post(SAGA_URL, json={
        "user_id": str(uuid.uuid4()), "total_cents": units * unit_price,
        "items_payload": [{
            "product_id": product_id, "seller_id": seller_id,
            "quantity": units, "price_cents": unit_price,
            "line_total_cents": units * unit_price}],
    }, headers=idem(), timeout=60.0)
    if res.status_code != 201:
        return None, None
    order_id = res.json()["id"]

    seller_order = None
    for _ in range(30):
        await asyncio.sleep(1)
        split = await client.get(f"{SAGA_URL}/{order_id}/seller-orders",
                                 timeout=60.0)
        candidate = split.json()["seller_orders"][0]
        if candidate["status"] != "PENDING":
            seller_order = candidate
            break
    if seller_order is None:
        return order_id, None

    for action in ("confirm", "dispatch", "deliver"):
        await client.post(
            f"{SAGA_URL}/{order_id}/seller-orders/{seller_order['id']}/{action}",
            json={"courier_name": "manual"} if action == "dispatch" else {},
            timeout=60.0)
    return order_id, seller_order


async def balance(client, seller_id):
    res = await client.get(f"{PAYMENT}/escrow/sellers/{seller_id}/balance",
                           timeout=60.0)
    return res.json()["owed_cents"] if res.status_code == 200 else None


async def wait_for_balance(client, seller_id, expected, attempts=40):
    import asyncio
    for _ in range(attempts):
        if await balance(client, seller_id) == expected:
            return True
        await asyncio.sleep(1)
    return False


async def main():
    print("=" * 62)
    print(" ESCROW LEDGER")
    print("=" * 62)
    print(f"-> {describe()}")

    async with httpx.AsyncClient() as client:

        print("\n1. Onboarding an active seller...")
        seller_id = await onboard_active_seller(
            client, f"Escrow {uuid.uuid4().hex[:4]}")
        check(seller_id is not None, "seller active",
              "could not onboard a seller")
        if seller_id is None:
            return finish()

        res = await client.get(f"{SELLER_URL}/{seller_id}/commission",
                               timeout=60.0)
        check(res.status_code == 200 and res.json()["commission_bps"] > 0,
              f"commission rate {res.json().get('commission_bps')}bps from "
              f"contract v{res.json().get('accepted_contract_version')}",
              f"no commission rate: {res.status_code} {res.text[:200]}")
        rate_bps = res.json()["commission_bps"]

        check(await balance(client, seller_id) == 0,
              "a new seller is owed nothing",
              "a brand new seller already has a balance")

        print("\n2. Delivering an order (2 x 1250)...")
        order_id, seller_order = await delivered_seller_order(
            client, seller_id, 2, 1250)
        check(seller_order is not None, "order delivered",
              "could not drive an order to delivery")
        if seller_order is None:
            return finish()

        collected = seller_order["subtotal_cents"]
        commission = collected * rate_bps // 10000
        owed = collected - commission

        landed = await wait_for_balance(client, seller_id, owed)
        check(landed,
              f"the liability appeared at delivery: {owed} owed "
              f"({collected} less {commission} commission)",
              f"expected {owed} owed after delivery, got "
              f"{await balance(client, seller_id)}. The liability must appear "
              f"when the courier collects, not when a payout is computed.")

        print("\n3. The ledger balances...")
        res = await client.get(f"{PAYMENT}/escrow/health", timeout=60.0)
        health = res.json()
        check(health["balanced"] and health["imbalance_cents"] == 0,
              f"every entry sums to zero; accounts {health['accounts']}",
              f"the ledger is out of balance by "
              f"{health.get('imbalance_cents')}: "
              f"{health.get('unbalanced_transactions')}")

        print("\n4. Booking the same delivery again...")
        res = await client.post(f"{PAYMENT}/escrow/delivery", json={
            "seller_id": seller_id, "seller_order_id": seller_order["id"],
            "collected_cents": collected}, timeout=60.0)
        check(res.status_code == 200 and res.json()["entries"] == 0,
              "a repeated booking is a no-op",
              f"a repeated delivery booking wrote "
              f"{res.json().get('entries')} entries")
        check(await balance(client, seller_id) == owed,
              "the balance did not move",
              f"a repeated booking changed the balance to "
              f"{await balance(client, seller_id)}; the seller would be paid "
              f"twice for one parcel")

        print("\n5. The courier remits...")
        res = await client.post(f"{PAYMENT}/escrow/settlement", json={
            "seller_id": seller_id, "seller_order_id": seller_order["id"],
            "remitted_cents": collected}, timeout=60.0)
        check(res.status_code == 200, "settlement booked",
              f"settlement failed: {res.status_code} {res.text[:200]}")
        check(await balance(client, seller_id) == owed,
              "settlement did not change what the seller is owed",
              f"the seller's balance moved on settlement to "
              f"{await balance(client, seller_id)}; that was decided at "
              f"delivery and a slow courier is not a reason to owe less")

        # Scoped to this parcel, not the whole account. COURIER_RECEIVABLE is
        # platform-wide and legitimately non-zero whenever any other order has
        # been delivered and not yet remitted -- which is the normal state of a
        # marketplace, and which an earlier version of this assertion mistook
        # for a bug.
        conn = await db("payment_ledger_db")
        receivable = await conn.fetchval(
            "SELECT coalesce(sum(amount_cents), 0) FROM ledger_entries "
            "WHERE seller_order_id = $1 AND account = 'COURIER_RECEIVABLE'",
            uuid.UUID(seller_order["id"]))
        check(receivable == 0,
              "the courier owes nothing further on this parcel",
              f"courier receivable for this parcel is {receivable} after a "
              f"full remittance")

        print("\n6. Paying the seller...")
        res = await client.post(f"{PAYMENT}/escrow/payout", json={
            "seller_id": seller_id, "amount_cents": owed}, timeout=60.0)
        check(res.status_code == 200, "payout booked",
              f"payout failed: {res.status_code} {res.text[:200]}")
        check(await balance(client, seller_id) == 0,
              "the seller is owed nothing further",
              f"after a full payout the seller is still owed "
              f"{await balance(client, seller_id)}")

        health = (await client.get(f"{PAYMENT}/escrow/health", timeout=60.0)).json()
        check(health["balanced"],
              f"the ledger still balances; accounts {health['accounts']}",
              f"the ledger is out of balance by {health['imbalance_cents']}")

        print("\n7. A second seller's money does not mix with the first...")
        other_id = await onboard_active_seller(
            client, f"Escrow2 {uuid.uuid4().hex[:4]}")
        if other_id is None:
            check(False, "", "could not onboard a second seller")
            return finish()
        _, other_order = await delivered_seller_order(client, other_id, 1, 900)
        if other_order is None:
            check(False, "", "could not deliver the second seller's order")
            return finish()

        other_owed = 900 - (900 * rate_bps // 10000)
        check(await wait_for_balance(client, other_id, other_owed),
              f"the second seller is owed {other_owed}",
              f"expected {other_owed} for the second seller, got "
              f"{await balance(client, other_id)}")
        check(await balance(client, seller_id) == 0,
              "the first seller's balance is untouched",
              f"the first seller's balance changed to "
              f"{await balance(client, seller_id)} when another seller was "
              f"paid; balances are mixing")

        print("\n8. Checking every stored transaction balances...")
        # Per-transaction, not just in total: two opposite errors would cancel
        # out in a grand total and both still be wrong.
        conn = await db("payment_ledger_db")
        unbalanced = await conn.fetchval(
            "SELECT count(*) FROM ("
            "  SELECT transaction_id FROM ledger_entries "
            "  GROUP BY transaction_id HAVING sum(amount_cents) <> 0) x")
        entries = await conn.fetchval("SELECT count(*) FROM ledger_entries")
        check(unbalanced == 0,
              f"all {entries} entries across balanced transactions",
              f"{unbalanced} transaction(s) do not balance; the books are "
              f"wrong and a grand total would hide it")

    await close_connections()
    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] The liability appeared at delivery, survived a repeated "
          "booking, was unaffected by settlement, cleared on payout, and the "
          "ledger balanced throughout.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
