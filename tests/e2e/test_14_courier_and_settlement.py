"""
A courier drives a seller order, and its remittance file is reconciled.

The unit tests pin the mapping and the reconciliation against fake inputs.
What they cannot show is the chain: a courier pushes a word, fulfillment-service
translates it, order-saga moves the seller order, and inventory-service moves
the units. Each link is correct in isolation while nothing happens end to end —
which is exactly how the seller-order commands sat unclaimed for a whole
lifecycle before test_13 caught it.

So this pushes real callbacks at a real shipment and counts stock.

The three assertions worth naming:

**An unknown word is never guessed.** A courier that invents a status must
leave the parcel where it is and flag itself for a human. Defaulting to
IN_TRANSIT hides a delivery; defaulting to DELIVERED consumes stock on a word
nobody has read.

**A failed attempt is not a return.** Couriers retry two or three times before
giving up. `not_home` must move nothing; only `returning` does.

**A settlement mismatch is classified, never absorbed.** The courier's file is
the only evidence the platform has that it was paid, so a short payment is
flagged rather than written down as the truth.

Not in CI: it needs order-saga, inventory-service and fulfillment-service.
"""

import sys
import uuid

import asyncpg
import httpx

from config import PLATFORM_SELLER_ID, db_url, describe, service_url

SAGA_URL = service_url("order-saga") + "/orders"
FULFILMENT = service_url("fulfillment-service")

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


# One connection per database, reused for the whole run.
#
# This test polls stock while waiting for an outbox command to land, and an
# earlier version opened a fresh asyncpg connection for every poll -- dozens in
# a few seconds. On a Windows host that reliably produced
# `ConnectionResetError: [WinError 64]` from the Docker port proxy partway
# through the run, which reads exactly like a platform fault and is not one.
_connections = {}


async def db(database):
    if database not in _connections:
        _connections[database] = await asyncpg.connect(db_url(database))
    return _connections[database]


async def close_connections():
    for connection in _connections.values():
        await connection.close()
    _connections.clear()


async def stock(product_id):
    conn = await db("inventory_db")
    row = await conn.fetchrow(
        "SELECT quantity_available, quantity_reserved FROM inventory_items "
        "WHERE product_id = $1", uuid.UUID(product_id))
    if row is None:
        return (None, None)
    return (row["quantity_available"], row["quantity_reserved"])


async def place_confirmed_order(client, units, unit_price=1250):
    """A COD seller order with stock held and the seller having confirmed."""
    import asyncio
    product_id = str(uuid.uuid4())
    conn = await db("inventory_db")
    await conn.execute(
        """INSERT INTO inventory_items
               (id, product_id, quantity_available, quantity_reserved, updated_at)
           VALUES ($1, $2, 10, 0, NOW())
           ON CONFLICT (product_id) DO UPDATE
               SET quantity_available = 10, quantity_reserved = 0""",
        uuid.uuid4(), uuid.UUID(product_id))

    res = await client.post(SAGA_URL, json={
        "user_id": str(uuid.uuid4()),
        "total_cents": units * unit_price,
        "items_payload": [{
            "product_id": product_id, "seller_id": PLATFORM_SELLER_ID,
            "quantity": units, "price_cents": unit_price,
            "line_total_cents": units * unit_price,
        }],
    }, headers={"Idempotency-Key": str(uuid.uuid4())}, timeout=60.0)
    if res.status_code != 201:
        return product_id, None, None
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
        return product_id, order_id, None

    await client.post(
        f"{SAGA_URL}/{order_id}/seller-orders/{seller_order['id']}/confirm",
        json={}, timeout=60.0)
    return product_id, order_id, seller_order


async def callback(client, provider, tracking, status):
    res = await client.post(f"{FULFILMENT}/couriers/{provider}/callback",
                            json={"tracking_code": tracking, "status": status},
                            timeout=60.0)
    return res.status_code, res.json()


async def wait_for_stock(product_id, expected, attempts=30):
    import asyncio
    for _ in range(attempts):
        if await stock(product_id) == expected:
            return True
        await asyncio.sleep(1)
    return False


async def main():
    print("=" * 62)
    print(" COURIER CONTRACT AND SETTLEMENT")
    print("=" * 62)
    print(f"-> {describe()}")

    async with httpx.AsyncClient() as client:

        print("\n1. Which couriers are configured...")
        res = await client.get(f"{FULFILMENT}/couriers", timeout=30.0)
        couriers = res.json().get("couriers", []) if res.status_code == 200 else []
        check("manual" in couriers,
              f"configured: {couriers}",
              f"the manual courier is not configured: {couriers}. A courier "
              f"with no mapping accepts callbacks and moves nothing.")
        if "manual" not in couriers:
            return finish()

        print("\n2. Placing and confirming a COD order (2 units)...")
        product_a, order_a, seller_order_a = await place_confirmed_order(client, 2)
        check(seller_order_a is not None,
              f"confirmed; stock {await stock(product_a)}",
              "could not place and confirm an order")
        if seller_order_a is None:
            return finish()
        soa = seller_order_a["id"]
        expected_cents = seller_order_a["subtotal_cents"]

        print("\n3. An unconfigured courier cannot take a parcel...")
        res = await client.post(f"{FULFILMENT}/shipments", json={
            "seller_order_id": soa, "order_id": order_a,
            "provider": "pathao", "tracking_code": "PTH-1",
            "cod_amount_cents": expected_cents}, timeout=30.0)
        check(res.status_code == 422,
              "an unmapped courier is refused (422)",
              f"an unmapped courier was accepted ({res.status_code}); its "
              f"callbacks would arrive and move nothing")

        print("\n4. Handing the parcel to a configured courier...")
        tracking = f"E2E-{uuid.uuid4().hex[:10].upper()}"
        res = await client.post(f"{FULFILMENT}/shipments", json={
            "seller_order_id": soa, "order_id": order_a,
            "provider": "manual", "tracking_code": tracking,
            "cod_amount_cents": expected_cents}, timeout=30.0)
        check(res.status_code == 201 and res.json()["status"] == "PICKUP_PENDING",
              "shipment created, awaiting pickup",
              f"could not create a shipment: {res.status_code} {res.text[:200]}")

        res = await client.post(f"{FULFILMENT}/shipments", json={
            "seller_order_id": soa, "order_id": order_a,
            "provider": "manual", "tracking_code": tracking + "-2",
            "cod_amount_cents": expected_cents}, timeout=30.0)
        check(res.status_code == 409,
              "a second parcel for the same seller order is refused",
              f"a seller order got two shipments ({res.status_code}); two "
              f"couriers would both collect cash for it")

        print("\n5. Callbacks that move the order, and callbacks that do not...")
        code, body = await callback(client, "manual", tracking, "Picked Up")
        check(code == 200 and body["canonical_status"] == "PICKED_UP"
              and body["action_result"] == "applied",
              "'Picked Up' dispatched the seller order",
              f"pickup did not dispatch: {code} {body}")

        code, body = await callback(client, "manual", tracking, "In Transit")
        check(code == 200 and body["action"] is None,
              "'In Transit' is informational and moves nothing",
              f"a movement update drove an action: {body}")

        code, body = await callback(client, "manual", tracking, "at_sorting_hub")
        check(code == 200 and body["unmapped"] is True
              and body["canonical_status"] is None,
              "an unmapped word is flagged, not guessed",
              f"an unknown status was mapped to "
              f"{body.get('canonical_status')}: guessing here consumes stock "
              f"on a word nobody has read")

        code, body = await callback(client, "manual", tracking, "not_home")
        check(code == 200 and body["canonical_status"] == "DELIVERY_FAILED"
              and body["action"] is None,
              "a failed attempt is not a return",
              f"a failed delivery attempt drove {body.get('action')}; couriers "
              f"retry before giving up, and this would send stock back for a "
              f"buyer who was simply out")

        check(await stock(product_a) == (8, 2),
              "stock is still held through all of that",
              f"stock moved before delivery: {await stock(product_a)}")

        print("\n6. Delivered...")
        code, body = await callback(client, "manual", tracking, "Delivered")
        check(code == 200 and body["action"] == "deliver"
              and body["action_result"] == "applied",
              "delivery drove the seller order",
              f"delivery did not apply: {code} {body}")
        check(await wait_for_stock(product_a, (8, 0)),
              f"stock consumed: {await stock(product_a)}",
              f"expected (8, 0) after delivery, got {await stock(product_a)}")

        code, body = await callback(client, "manual", tracking, "Delivered")
        check(code == 200 and body["action_result"] == "not_applicable",
              "a resent delivery callback is a benign no-op",
              f"a repeated delivery returned {body.get('action_result')}; "
              f"couriers resend, and a second consume would take more units "
              f"out of the warehouse")

        print("\n7. The courier remits...")
        res = await client.post(f"{FULFILMENT}/settlements", json={
            "provider": "manual",
            "batch_reference": f"BATCH-OK-{tracking[-6:]}",
            "rows": [{"reference": tracking, "collected_cents": expected_cents}],
        }, timeout=60.0)
        summary = res.json()
        check(res.status_code == 200 and summary["matched"] == 1
              and summary["needs_attention"] == 0,
              f"an exact remittance reconciles ({expected_cents})",
              f"an exact remittance did not match: {summary}")

        print("\n8. A short payment on a different order...")
        product_b, order_b, seller_order_b = await place_confirmed_order(client, 3)
        if seller_order_b is None:
            check(False, "", "could not place the second order")
            return finish()
        sob = seller_order_b["id"]
        expected_b = seller_order_b["subtotal_cents"]
        tracking_b = f"E2E-{uuid.uuid4().hex[:10].upper()}"

        await client.post(f"{FULFILMENT}/shipments", json={
            "seller_order_id": sob, "order_id": order_b, "provider": "manual",
            "tracking_code": tracking_b, "cod_amount_cents": expected_b},
            timeout=30.0)
        await callback(client, "manual", tracking_b, "Picked Up")
        await callback(client, "manual", tracking_b, "Delivered")
        await wait_for_stock(product_b, (7, 0))

        res = await client.post(f"{FULFILMENT}/settlements", json={
            "provider": "manual",
            "batch_reference": f"BATCH-SHORT-{tracking_b[-6:]}",
            "rows": [{"reference": tracking_b,
                      "collected_cents": expected_b - 500}],
        }, timeout=60.0)
        summary = res.json()
        check(res.status_code == 200 and summary["needs_attention"] == 1
              and summary["outcomes"].get("short") == 1,
              f"a short payment is flagged, variance "
              f"{summary.get('variance_cents')}",
              f"a short payment was not flagged: {summary}. The courier's file "
              f"is not the authority on what it owes.")
        check(summary.get("variance_cents") == -500,
              "the shortfall is reported exactly",
              f"variance was {summary.get('variance_cents')}, expected -500")

        print("\n9. Cash for an order the platform does not have...")
        res = await client.post(f"{FULFILMENT}/settlements", json={
            "provider": "manual",
            "batch_reference": f"BATCH-GHOST-{uuid.uuid4().hex[:6]}",
            "rows": [{"reference": "NOT-A-REAL-PARCEL", "collected_cents": 999}],
        }, timeout=60.0)
        summary = res.json()
        check(res.status_code == 200
              and summary["outcomes"].get("unknown_order") == 1,
              "an unknown reference is never booked",
              f"an unknown reference was not flagged: {summary}")

        print("\n10. Checking nothing was quietly discarded...")
        # Rows that did not reconcile are stored too: a rejected row that is
        # forgotten is a dispute nobody can reconstruct.
        conn = await db("fulfillment_db")
        stored = await conn.fetchval(
            "SELECT count(*) FROM settlement_rows WHERE outcome <> 'matched'")
        # The event, not the column.
        #
        # `shipments.unmapped` is current state -- "the last thing this courier
        # said was not understood" -- and it is cleared by the next
        # recognisable status, which is correct: a parcel that carried on
        # moving is not stuck. The durable record of the unknown word is the
        # ShipmentStatusUnmapped event, and that is what an operator's queue
        # would be built from.
        unmapped = await conn.fetchval(
            "SELECT count(*) FROM outbox_messages "
            "WHERE type = 'ShipmentStatusUnmapped' "
            "  AND payload->>'shipment_id' = ("
            "        SELECT id::text FROM shipments WHERE tracking_code = $1)",
            tracking)
        check(stored >= 2,
              f"{stored} non-matching row(s) kept for reconciliation",
              "non-matching settlement rows were not stored; a rejected row "
              "that is forgotten is a dispute nobody can reconstruct")
        check(unmapped == 1,
              f"the unknown word left {unmapped} durable record for a human",
              f"{unmapped} ShipmentStatusUnmapped events for a callback that "
              f"was not understood; without one, a courier inventing a status "
              f"is invisible")

    await close_connections()
    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] A courier's own words drove a seller order to delivery, "
          "an unknown word was parked rather than guessed, and the remittance "
          "file was reconciled rather than believed.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
