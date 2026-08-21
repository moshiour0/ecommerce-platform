"""
One cart, two sellers, two seller orders.

The unit tests in tests/unit/test_order_split.py pin the grouping and the
derived status against fake lines. What they cannot show is that the whole
chain agrees: that catalog returns a `seller_id`, that bff-checkout carries it
onto every validated line, that order-saga splits on it, and that the money
still adds up at the far end.

That chain is the point. Each link looks correct on its own while the order
silently ends up attributed to one seller — or to none — and the failure only
becomes visible when somebody is not paid.

So this drives a real checkout: two onboarded sellers, one product each, one
cart with both, one checkout. Then it asks order-saga what it made of it.

The assertion that matters most is conservation. The seller subtotals must sum
to the goods total exactly. Money that goes missing in a split goes missing
quietly, because every individual seller order looks plausible by itself.

Not in CI: it needs seller-service, catalog-service, pricing-service,
cart-service, inventory-service, bff-checkout and order-saga, which is most of
the platform.
"""

import sys
import uuid

import asyncpg
import httpx

from config import connect_with_retry, db_url, describe, new_client, service_url

CATALOG_URL = service_url("catalog-service") + "/products"
PRICING_URL = service_url("pricing-service") + "/prices"
CART_URL = service_url("cart-service") + "/cart"
SELLER_URL = service_url("seller-service") + "/sellers"
SAGA_URL = service_url("order-saga") + "/orders"
BFF_CHECKOUT_URL = service_url("bff-checkout") + "/api/checkout"

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def idem():
    return {"Idempotency-Key": str(uuid.uuid4())}


async def onboard_seller(client, name):
    """A seller driven all the way to active, because only those may list."""
    res = await client.post(SELLER_URL, json={
        "legal_name": f"{name} Ltd", "display_name": name,
        "contact_email": f"{name.lower().replace(' ', '')}@example.com",
        "city": "Dhaka", "district": "Dhaka",
    }, headers=idem(), timeout=30.0)
    if res.status_code != 201:
        return None
    seller_id = res.json()["id"]

    await client.post(f"{SELLER_URL}/{seller_id}/documents", json={
        "documents": [{"document_type": d, "media_id": str(uuid.uuid4())}
                      for d in ("national_id", "trade_licence")]}, timeout=30.0)
    await client.post(f"{SELLER_URL}/{seller_id}/review", timeout=30.0)
    await client.post(f"{SELLER_URL}/{seller_id}/review/result",
                      json={"approved": True}, timeout=30.0)

    contract = await client.get(service_url("seller-service") + "/contract",
                                timeout=30.0)
    await client.post(f"{SELLER_URL}/{seller_id}/contract", json={
        "version": contract.json()["current_contract_version"]}, timeout=30.0)
    return seller_id


async def seed_product(client, seller_id, category_id, name, price_cents):
    """A listable product owned by this seller, priced and in stock."""
    res = await client.post(f"{CATALOG_URL}/", json={
        "seller_id": seller_id, "category_id": category_id,
        "sku": f"SKU-{uuid.uuid4().hex[:8].upper()}", "name": name,
        "description": "seeded by test_12", "price_cents": price_cents,
        "is_active": True,
    }, headers=idem(), timeout=30.0)
    if res.status_code != 201:
        print(f"      [!] could not create product for {seller_id}: "
              f"{res.status_code} {res.text[:200]}")
        return None
    product_id = res.json()["id"]

    # Generous, and retried once. This seeds a fixture rather than testing
    # anything, and a slow first write to a cold service should not read as a
    # failure of the split.
    for attempt in range(2):
        try:
            await client.post(f"{PRICING_URL}/", json={
                "product_id": product_id, "base_price_cents": price_cents,
                "currency": "BDT"}, headers=idem(), timeout=60.0)
            break
        except httpx.HTTPError as e:
            if attempt == 1:
                print(f"      [!] pricing seed failed for {product_id}: {e}")

    conn = await connect_with_retry(db_url("inventory_db"))
    try:
        await conn.execute(
            """INSERT INTO inventory_items
                   (id, product_id, quantity_available, quantity_reserved, updated_at)
               VALUES ($1, $2, $3, 0, NOW())
               ON CONFLICT (product_id) DO UPDATE
                   SET quantity_available = EXCLUDED.quantity_available""",
            uuid.uuid4(), uuid.UUID(product_id), 50)
    finally:
        await conn.close()
    return product_id


async def main():
    print("=" * 62)
    print(" ORDER SPLITTING ACROSS SELLERS")
    print("=" * 62)
    print(f"-> {describe()}")

    conn = await connect_with_retry(db_url("catalog_db"))
    try:
        row = await conn.fetchrow("SELECT id FROM categories LIMIT 1")
    finally:
        await conn.close()
    if row is None:
        print("   [!] no category to file products under")
        return 1
    category_id = str(row["id"])

    async with new_client() as client:
        print("\n1. Onboarding two sellers...")
        seller_a = await onboard_seller(client, f"Split A {uuid.uuid4().hex[:4]}")
        seller_b = await onboard_seller(client, f"Split B {uuid.uuid4().hex[:4]}")
        check(seller_a and seller_b, "both sellers reached active",
              "could not onboard two sellers")
        if not (seller_a and seller_b):
            return finish()

        print("\n2. One product each...")
        # Different prices and quantities so a split that mixed the two up
        # produces visibly wrong subtotals rather than a coincidence.
        product_a = await seed_product(client, seller_a, category_id,
                                       "Seller A Keyboard", 1200)
        product_b = await seed_product(client, seller_b, category_id,
                                       "Seller B Mouse", 3400)
        check(product_a and product_b, "both products listed",
              "an active seller could not list a product")
        if not (product_a and product_b):
            return finish()

        print("\n3. One cart holding both...")
        user_id = str(uuid.uuid4())
        quantity_a, quantity_b = 2, 1
        for product_id, quantity in ((product_a, quantity_a), (product_b, quantity_b)):
            res = await client.post(f"{CART_URL}/{user_id}/items", json={
                "item": {"product_id": product_id, "quantity": quantity}},
                headers=idem(), timeout=30.0)
            if res.status_code >= 400:
                check(False, "", f"add-to-cart failed: {res.status_code} "
                                 f"{res.text[:200]}")
                return finish()
        print(f"      user {user_id[:8]}: {quantity_a} x A, {quantity_b} x B")

        print("\n4. One checkout...")
        res = await client.post(f"{BFF_CHECKOUT_URL}/{user_id}", json={
            "telemetry": {"device_id": "test-device", "ip": "127.0.0.1"},
            "destination_region": "BD-Dhaka", "weight_grams": 800,
        }, headers=idem(), timeout=30.0)
        check(res.status_code == 201,
              f"checkout accepted (HTTP {res.status_code})",
              f"checkout was rejected: {res.status_code} {res.text[:300]}")
        if res.status_code != 201:
            return finish()
        order = res.json()
        order_id = order["id"]
        print(f"      order {order_id[:8]}  total {order['total_cents']}")

        print("\n5. Asking order-saga what it made of it...")
        res = await client.get(f"{SAGA_URL}/{order_id}/seller-orders", timeout=30.0)
        check(res.status_code == 200, "seller orders readable",
              f"could not read the split: {res.status_code} {res.text[:200]}")
        if res.status_code != 200:
            return finish()
        split = res.json()

        for seller_order in split["seller_orders"]:
            print(f"      seller {seller_order['seller_id'][:8]}...  "
                  f"{seller_order['status']:9} subtotal="
                  f"{seller_order['subtotal_cents']:>6}  "
                  f"items={seller_order['item_count']}")

        check(split["seller_order_count"] == 2,
              "one cart produced two seller orders",
              f"expected 2 seller orders, got {split['seller_order_count']}. "
              f"A marketplace order that does not split cannot be collected, "
              f"paid out or returned per seller.")

        by_seller = {so["seller_id"]: so for so in split["seller_orders"]}
        check(set(by_seller) == {seller_a, seller_b},
              "each seller order belongs to the right seller",
              f"seller orders were attributed to {sorted(by_seller)} rather "
              f"than to {sorted([seller_a, seller_b])}")

        if set(by_seller) == {seller_a, seller_b}:
            expected_a = 1200 * quantity_a
            expected_b = 3400 * quantity_b
            check(by_seller[seller_a]["subtotal_cents"] == expected_a,
                  f"seller A's subtotal is {expected_a}",
                  f"seller A's subtotal is "
                  f"{by_seller[seller_a]['subtotal_cents']}, expected "
                  f"{expected_a}")
            check(by_seller[seller_b]["subtotal_cents"] == expected_b,
                  f"seller B's subtotal is {expected_b}",
                  f"seller B's subtotal is "
                  f"{by_seller[seller_b]['subtotal_cents']}, expected "
                  f"{expected_b}")
            check(by_seller[seller_a]["item_count"] == quantity_a and
                  by_seller[seller_b]["item_count"] == quantity_b,
                  "item counts count units, not lines",
                  f"item counts were "
                  f"{by_seller[seller_a]['item_count']}/"
                  f"{by_seller[seller_b]['item_count']}, expected "
                  f"{quantity_a}/{quantity_b}")

        print("\n6. Checking the money is conserved...")
        # The assertion that matters. A split that drops a line drops a
        # seller's money, and each seller order still looks correct alone.
        goods = 1200 * quantity_a + 3400 * quantity_b
        check(split["goods_subtotal_cents"] == goods,
              f"seller subtotals sum to the goods total ({goods})",
              f"the seller subtotals sum to {split['goods_subtotal_cents']} "
              f"but the goods came to {goods}: money went missing in the split")
        check(split["goods_subtotal_cents"] <= order["total_cents"],
              "the goods total does not exceed the order total",
              f"goods {split['goods_subtotal_cents']} exceed the order total "
              f"{order['total_cents']}")

        print("\n7. Checking the parent status is derived, not stored...")
        check(split["derived_status"] is not None,
              f"derived status is {split['derived_status']}",
              "the derived status could not be computed, which means a seller "
              "order carries a status this version does not recognise")
        check(split["complete"] is False,
              "a fresh order is not complete",
              "a brand new order reported itself complete")

        print("\n8. Checking every seller order left an event...")
        conn = await connect_with_retry(db_url("order_db"))
        try:
            rows = await conn.fetch(
                "SELECT payload->>'seller_id' AS seller_id, "
                "       payload->>'subtotal_cents' AS subtotal "
                "FROM outbox_messages "
                "WHERE type = 'SellerOrderCreated' "
                "  AND payload->>'order_id' = $1", order_id)
            orphans = await conn.fetchval(
                "SELECT count(*) FROM seller_orders so "
                "LEFT JOIN order_saga_states o ON o.id = so.order_id "
                "WHERE o.id IS NULL")
        finally:
            await conn.close()

        check(len(rows) == 2,
              "one SellerOrderCreated per seller order",
              f"{len(rows)} SellerOrderCreated events for 2 seller orders. A "
              f"seller order nothing announced is an order no seller is told "
              f"to ship.")
        check({r["seller_id"] for r in rows} == {seller_a, seller_b},
              "the events name the right sellers",
              f"events named {sorted(r['seller_id'] for r in rows)}")
        check(orphans == 0,
              "no seller orders without a parent order",
              f"{orphans} seller order(s) have no parent: a refused split left "
              f"rows behind")

    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] One cart across two sellers became two seller orders, "
          "the money was conserved, and each was announced.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
