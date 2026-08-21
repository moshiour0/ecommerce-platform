"""
A cash-on-delivery order, from checkout to settlement, and the two ways it can
end instead.

The unit tests pin the transition table and its inventory effects against fake
states. What they cannot show is that the effects actually reach the
warehouse: order-saga emits a command, the dispatcher claims it, and
inventory-service moves the units. Every one of those links looked correct in
isolation while nothing moved at all -- the dispatcher's claim query filtered
on `aggregate_type = 'OrderSaga'`, so the seller-order commands sat unclaimed
forever. Every transition applied, every event published, and not one unit
changed hands.

So this counts stock at every step.

Three paths, and the arithmetic differs in a way that matters:

  delivered   units leave the building     available unchanged, reserved -N
  returned    units go back on the shelf   available +N,        reserved -N
  cancelled   the hold is dropped          available +N,        reserved -N

Delivery is the only one that does not conserve the total, and that is the
point: the goods are in a customer's hands. A delivery implemented as a
release would put sold goods back on sale, and a dispatch implemented as a
consume would lose everything that comes back.

It also asserts the thing that was quietly wrong before COD existed: a
cash-on-delivery order must never emit a ChargePaymentCommand. No card was
presented, and the goods are not delivered yet.
"""

import sys
import uuid

import asyncpg
import httpx

from config import db_url, describe, service_url

SAGA_URL = service_url("order-saga") + "/orders"

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


async def stock(product_id):
    conn = await asyncpg.connect(db_url("inventory_db"))
    try:
        row = await conn.fetchrow(
            "SELECT quantity_available, quantity_reserved FROM inventory_items "
            "WHERE product_id = $1", uuid.UUID(product_id))
        return (row["quantity_available"], row["quantity_reserved"]) if row \
            else (None, None)
    finally:
        await conn.close()


async def seed_stock(product_id, units):
    conn = await asyncpg.connect(db_url("inventory_db"))
    try:
        await conn.execute(
            """INSERT INTO inventory_items
                   (id, product_id, quantity_available, quantity_reserved, updated_at)
               VALUES ($1, $2, $3, 0, NOW())
               ON CONFLICT (product_id) DO UPDATE
                   SET quantity_available = EXCLUDED.quantity_available,
                       quantity_reserved = 0""",
            uuid.uuid4(), uuid.UUID(product_id), units)
    finally:
        await conn.close()


async def charge_commands(order_id):
    conn = await asyncpg.connect(db_url("order_db"))
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM outbox_messages "
            "WHERE type = 'ChargePaymentCommand' "
            "  AND payload->>'order_id' = $1", order_id)
    finally:
        await conn.close()


async def wait_for_stock(product_id, expected, attempts=30):
    """Stock movements go through the outbox and the dispatcher, so they lag."""
    import asyncio
    for _ in range(attempts):
        if await stock(product_id) == expected:
            return True
        await asyncio.sleep(1)
    return False


async def place_order(client, units):
    """A COD order holding `units` of a fresh product, once stock is held."""
    import asyncio
    product_id = str(uuid.uuid4())
    await seed_stock(product_id, 10)

    res = await client.post(SAGA_URL, json={
        "user_id": str(uuid.uuid4()),
        "total_cents": units * 1000,
        "items_payload": [{
            "product_id": product_id, "seller_id": str(uuid.uuid4()),
            "quantity": units, "price_cents": 1000,
            "line_total_cents": units * 1000,
        }],
    }, headers={"Idempotency-Key": str(uuid.uuid4())}, timeout=30.0)
    if res.status_code != 201:
        return product_id, None, None, res

    order_id = res.json()["id"]
    seller_order = None
    for _ in range(30):
        await asyncio.sleep(1)
        split = await client.get(f"{SAGA_URL}/{order_id}/seller-orders",
                                 timeout=30.0)
        candidate = split.json()["seller_orders"][0]
        if candidate["status"] != "PENDING":
            seller_order = candidate
            break
    return product_id, order_id, seller_order, res


async def act(client, order_id, seller_order_id, action, **body):
    res = await client.post(
        f"{SAGA_URL}/{order_id}/seller-orders/{seller_order_id}/{action}",
        json=body, timeout=30.0)
    return res.status_code, res.json()


async def main():
    print("=" * 62)
    print(" CASH ON DELIVERY LIFECYCLE")
    print("=" * 62)
    print(f"-> {describe()}")

    async with httpx.AsyncClient() as client:

        # -------------------------------------------------------------- A
        print("\n1. Placing a COD order (3 units)...")
        product_a, order_a, seller_order_a, res = await place_order(client, 3)
        check(order_a is not None and seller_order_a is not None,
              f"order placed and stock held: {await stock(product_a)}",
              f"could not place a COD order: {res.status_code} "
              f"{res.text[:200]}")
        if not (order_a and seller_order_a):
            return finish()
        soa = seller_order_a["id"]

        check(await stock(product_a) == (7, 3),
              "3 units moved from available to reserved",
              f"expected (7, 3) after reservation, got {await stock(product_a)}")

        print("\n2. Checking no card was charged...")
        # No card was presented and the goods are not delivered. This arm of
        # the card table was simply wrong for COD.
        charges = await charge_commands(order_a)
        check(charges == 0,
              "no ChargePaymentCommand was emitted",
              f"{charges} charge command(s) for a cash-on-delivery order")

        print("\n3. Refusing what cannot happen yet...")
        for action, expected in (("deliver", 409), ("settle", 409),
                                 ("complete_return", 409), ("teleport", 422)):
            code, body = await act(client, order_a, soa, action)
            check(code == expected,
                  f"{action} refused with {code}",
                  f"{action} returned {code}, expected {expected}: "
                  f"{str(body.get('detail'))[:100]}")

        print("\n4. Confirm, dispatch...")
        for action, kwargs in (("confirm", {}),
                               ("dispatch", {"courier_name": "Pathao",
                                             "tracking_code": "PTH-E2E"})):
            code, body = await act(client, order_a, soa, action, **kwargs)
            check(code == 200, f"{action} -> {body.get('status')}",
                  f"{action} returned {code}: {str(body.get('detail'))[:150]}")

        check(await stock(product_a) == (7, 3),
              "a dispatched parcel is still held, not consumed",
              f"stock moved on dispatch: {await stock(product_a)}. A parcel "
              f"in a van is still returnable and must stay reserved.")

        print("\n5. A saga cannot undo a van...")
        code, body = await act(client, order_a, soa, "cancel",
                               reason="buyer changed their mind")
        check(code == 409,
              "cancel is refused once dispatched (409)",
              f"a dispatched seller order was cancelled: {code}. The goods are "
              f"physically moving; the only route back is a return.")

        print("\n6. Delivered: the units leave the building...")
        code, body = await act(client, order_a, soa, "deliver")
        check(code == 200 and body.get("inventory_effect") == "consume",
              "delivery consumes",
              f"deliver returned {code} effect={body.get('inventory_effect')}")
        moved = await wait_for_stock(product_a, (7, 0))
        check(moved,
              f"stock is now {await stock(product_a)}: reserved retired, "
              f"available unchanged",
              f"expected (7, 0) after delivery, got {await stock(product_a)}. "
              f"Available must NOT rise -- the buyer has the goods.")

        code, body = await act(client, order_a, soa, "settle")
        check(code == 200 and body.get("status") == "SETTLED",
              "settled once the courier remitted",
              f"settle returned {code}")

        split = (await client.get(f"{SAGA_URL}/{order_a}/seller-orders",
                                  timeout=30.0)).json()
        check(split["derived_status"] == "SETTLED" and split["complete"],
              "the buyer-facing status derives to SETTLED and is complete",
              f"derived {split['derived_status']}, complete "
              f"{split['complete']}")

        # -------------------------------------------------------------- B
        print("\n7. A second order, refused at the door (2 units)...")
        product_b, order_b, seller_order_b, res = await place_order(client, 2)
        if not (order_b and seller_order_b):
            check(False, "", f"could not place the second order: {res.text[:200]}")
            return finish()
        sob = seller_order_b["id"]

        await act(client, order_b, sob, "confirm")
        await act(client, order_b, sob, "dispatch", courier_name="RedX",
                  tracking_code="RDX-E2E")

        code, body = await act(client, order_b, sob, "mark_rto")
        check(code == 422,
              "an RTO with no reason is refused",
              f"an unexplained RTO was accepted ({code}); refusal reasons are "
              f"the input to refusal-risk scoring")

        code, body = await act(client, order_b, sob, "mark_rto",
                               reason="buyer refused at door")
        check(code == 200 and body.get("status") == "RTO_IN_TRANSIT",
              "marked as coming back",
              f"mark_rto returned {code}")

        check(await stock(product_b) == (8, 2),
              "stock stays held while the parcel is in transit back",
              f"stock moved during RTO: {await stock(product_b)}")

        code, body = await act(client, order_b, sob, "complete_return")
        check(code == 200 and body.get("inventory_effect") == "release",
              "the return releases",
              f"complete_return returned {code} "
              f"effect={body.get('inventory_effect')}")
        restored = await wait_for_stock(product_b, (10, 0))
        check(restored,
              f"stock is back to {await stock(product_b)}: on the shelf and "
              f"sellable again",
              f"expected (10, 0) after the return, got "
              f"{await stock(product_b)}. Returned goods must go back on sale.")

        # -------------------------------------------------------------- C
        print("\n8. A third order, cancelled before dispatch (4 units)...")
        product_c, order_c, seller_order_c, res = await place_order(client, 4)
        if not (order_c and seller_order_c):
            check(False, "", f"could not place the third order: {res.text[:200]}")
            return finish()
        soc = seller_order_c["id"]

        code, body = await act(client, order_c, soc, "cancel")
        check(code == 422, "a cancellation with no reason is refused",
              f"an unexplained cancellation was accepted ({code})")

        code, body = await act(client, order_c, soc, "cancel",
                               reason="seller out of stock")
        check(code == 200 and body.get("inventory_effect") == "release",
              "cancelling before dispatch releases the hold",
              f"cancel returned {code} effect={body.get('inventory_effect')}")
        released = await wait_for_stock(product_c, (10, 0))
        check(released,
              f"stock is back to {await stock(product_c)}",
              f"expected (10, 0) after cancellation, got "
              f"{await stock(product_c)}")

        print("\n9. Checking nothing is left holding stock...")
        # Every one of the three orders reached a terminal state, so none of
        # them may still be holding units. This is the leak the lifecycle was
        # written to close: before it, a delivered order's reservation stayed
        # 'held' forever and quantity_reserved only ever grew.
        conn = await asyncpg.connect(db_url("inventory_db"))
        try:
            still_held = await conn.fetchval(
                "SELECT count(*) FROM inventory_reservations "
                "WHERE status = 'held' AND order_id = ANY($1::text[])",
                [order_a, order_b, order_c])
        finally:
            await conn.close()
        check(still_held == 0,
              "no terminal order is still holding stock",
              f"{still_held} reservation(s) still held by orders that have "
              f"finished")

    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] A COD order reached settlement with its stock consumed, a "
          "refused one put its goods back on the shelf, a cancelled one "
          "released its hold, and no card was ever charged.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
