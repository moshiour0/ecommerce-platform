"""
What a suspension actually stops.

Listing enforcement was creation-only: `catalog-service` asks seller-service
whether a seller may list at the moment a product is created, and nothing ever
asked again. So suspending a seller left their entire existing catalogue live
-- visible in search, and, far worse, still buyable. A suspension that does not
stop orders is not a suspension.

The assertions worth naming:

**Checkout refuses.** This is the half that matters. Search visibility is
cosmetic next to a platform that keeps taking money for a banned merchant.

**Search hides them, but the products are not deleted.** A suspension is
usually temporary, and a projection that destroyed the documents would make
reinstating a seller a reindex rather than a flag.

**Reinstating brings them back.** The whole point of a flag rather than a
delete.

**A seller the projection has not reached is still visible.** Written as "not
explicitly false" rather than "is true", so deploying this could not empty the
catalogue. Suspension hides a seller only once the platform positively knows
they are suspended.

Not in CI: it needs seller-service, catalog, cart, bff-checkout, Elasticsearch
and the stream-processor projection.
"""

import asyncio
import sys
import uuid

from config import connect_with_retry, db_url, describe, new_client, service_url

SELLER = service_url("seller-service") + "/sellers"
CATALOG = service_url("catalog-service") + "/products"
CART = service_url("cart-service") + "/cart"
CHECKOUT = service_url("bff-checkout") + "/api/checkout"
ES = "http://localhost:9200"

# How long to let the projection carry a suspension into the read model. It is
# an event through Kafka and an update-by-query, so it is not instant.
PROJECTION_WAIT_SECONDS = 60

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] A suspended seller could not be bought from, disappeared "
          "from search without losing their catalogue, and came back intact.")
    return 0


async def visible_count(client, seller_id):
    """Products of this seller that search would return."""
    res = await client.post(f"{ES}/products/_search", json={
        "size": 0, "query": {"bool": {
            "must": [{"term": {"seller_id": seller_id}}],
            "filter": [{"bool": {"must_not": [
                {"term": {"seller_may_sell": False}}]}}]}}})
    return res.json()["hits"]["total"]["value"]


async def indexed_count(client, seller_id):
    res = await client.post(f"{ES}/products/_search", json={
        "size": 0, "query": {"term": {"seller_id": seller_id}}})
    return res.json()["hits"]["total"]["value"]


async def wait_for_visibility(client, seller_id, expected, seconds):
    for _ in range(seconds):
        if await visible_count(client, seller_id) == expected:
            return True
        await asyncio.sleep(1)
    return False


async def main():
    print("=" * 62)
    print(" WHAT A SUSPENSION STOPS")
    print("=" * 62)
    print(f"-> {describe()}")

    async with new_client() as client:
        print("\n1. Finding an active seller with an indexed catalogue...")
        res = await client.post(f"{ES}/products/_search", json={
            "size": 0, "aggs": {"s": {"terms": {
                "field": "seller_id", "size": 20}}}})
        buckets = res.json()["aggregations"]["s"]["buckets"]

        seller_id = None
        product_id = None
        for bucket in buckets:
            candidate = bucket["key"]
            # The platform's own first-party shop is skipped: suspending it
            # would hide most of the catalogue from every other test running
            # against this stack.
            if candidate.startswith("00000000-0000-0000-0000"):
                continue
            profile = await client.get(f"{SELLER}/{candidate}")
            if profile.status_code != 200:
                continue
            if profile.json().get("status") != "active":
                continue
            hit = await client.post(f"{ES}/products/_search", json={
                "size": 1, "query": {"term": {"seller_id": candidate}},
                "_source": ["product_id"]})
            hits = hit.json()["hits"]["hits"]
            if hits:
                seller_id = candidate
                product_id = hits[0]["_source"]["product_id"]
                break

        if seller_id is None:
            check(False, "", "no active non-platform seller with indexed "
                             "products; cannot exercise a suspension")
            return finish()

        before_visible = await visible_count(client, seller_id)
        before_indexed = await indexed_count(client, seller_id)
        check(before_visible > 0,
              f"seller {seller_id[:8]} has {before_visible} visible product(s)",
              f"the chosen seller has nothing visible to hide")

        print("\n2. A buyer can check out with their product today...")
        buyer = str(uuid.uuid4())
        # Stock, so the refusal under test is the seller's standing and not an
        # empty shelf.
        conn = await connect_with_retry(db_url("inventory_db"))
        try:
            await conn.execute(
                """
                INSERT INTO inventory_items (product_id, total_quantity, reserved_quantity)
                VALUES ($1::uuid, 50, 0)
                ON CONFLICT (product_id)
                DO UPDATE SET total_quantity = 50, reserved_quantity = 0
                """, product_id)
        except Exception as exc:
            print(f"      [..]   could not seed stock ({type(exc).__name__}); "
                  f"continuing")
        finally:
            await conn.close()

        # And a price, for a reason worth recording: bff-checkout validates
        # catalog *and* pricing before it ever asks about the seller, so a
        # product with no published price 404s early and the suspension check
        # is never reached. The first run of this test picked exactly such a
        # product and reported the same 404 whether the seller was suspended or
        # not -- a test that could not tell the two apart while appearing to
        # exercise both.
        conn = await connect_with_retry(db_url("pricing_db"))
        try:
            # No ON CONFLICT: prices has no unique constraint on product_id
            # (it is an append-style table keyed on its own id), so the insert
            # is guarded by NOT EXISTS instead. `id` has no default either.
            await conn.execute(
                """
                INSERT INTO prices (id, product_id, base_price_cents, currency,
                                    effective_date, created_at)
                SELECT gen_random_uuid(), $1::uuid, 1500, 'BDT', NOW(), NOW()
                WHERE NOT EXISTS (
                    SELECT 1 FROM prices WHERE product_id = $1::uuid)
                """, product_id)
        except Exception as exc:
            print(f"      [..]   could not seed a price ({type(exc).__name__}); "
                  f"continuing")
        finally:
            await conn.close()

        res = await client.post(f"{CART}/{buyer}/items",
                                json={"item": {"product_id": product_id,
                                               "quantity": 1}},
                                headers={"Idempotency-Key": str(uuid.uuid4())})
        check(res.status_code < 400,
              "the product went into a cart",
              f"could not add to cart: {res.status_code} {res.text[:150]}")

        res = await client.post(f"{CHECKOUT}/{buyer}", json={
            "telemetry": {"device_id": "suspension-test", "ip": "127.0.0.1"},
            "destination_region": "BD-Dhaka", "weight_grams": 500},
            headers={"Idempotency-Key": str(uuid.uuid4())})
        baseline_ok = res.status_code < 400
        check(baseline_ok,
              f"checkout succeeded while the seller was active "
              f"({res.status_code})",
              f"checkout failed before the seller was even suspended "
              f"({res.status_code} {res.text[:150]}) -- this test cannot tell "
              f"a suspension from a pre-existing problem")

        print("\n3. Suspending the seller...")
        res = await client.post(f"{SELLER}/{seller_id}/suspend",
                                json={"reason": "e2e: what a suspension stops"})
        check(res.status_code == 200, "suspended",
              f"could not suspend: {res.status_code} {res.text[:150]}")

        res = await client.get(f"{SELLER}/{seller_id}/permission")
        permission = res.json()
        check(permission.get("may_receive_orders") is False,
              "seller-service reports may_receive_orders=false",
              f"a suspended seller may still receive orders: {permission}")

        print("\n4. Checkout now refuses -- the half that matters...")
        buyer2 = str(uuid.uuid4())
        res = await client.post(f"{CART}/{buyer2}/items",
                                json={"item": {"product_id": product_id,
                                               "quantity": 1}},
                                headers={"Idempotency-Key": str(uuid.uuid4())})
        res = await client.post(f"{CHECKOUT}/{buyer2}", json={
            "telemetry": {"device_id": "suspension-test", "ip": "127.0.0.1"},
            "destination_region": "BD-Dhaka", "weight_grams": 500},
            headers={"Idempotency-Key": str(uuid.uuid4())})
        check(res.status_code == 409,
              f"checkout was refused with 409 -- a suspension that does not "
              f"stop orders is not a suspension",
              f"a suspended seller's product was still checked out: "
              f"{res.status_code} {res.text[:200]}")

        body = res.json() if res.status_code == 409 else {}
        detail = str(body.get("detail", ""))
        check("suspend" not in detail.lower() and "ban" not in detail.lower(),
              "the refusal does not leak the seller's standing to the buyer",
              f"the refusal names the seller's status: {detail}")

        print("\n5. Search hides them, without deleting anything...")
        hidden = await wait_for_visibility(client, seller_id, 0,
                                           PROJECTION_WAIT_SECONDS)
        check(hidden,
              "their products are no longer returned by search",
              f"after {PROJECTION_WAIT_SECONDS}s "
              f"{await visible_count(client, seller_id)} product(s) are still "
              f"visible. Check SELLER_SIGNAL_EVENTS in event_rules -- main.py "
              f"drops unlisted events before the router runs.")

        still_indexed = await indexed_count(client, seller_id)
        check(still_indexed == before_indexed,
              f"all {still_indexed} documents are still in the index -- hidden "
              f"by a flag, not destroyed",
              f"documents were lost: {before_indexed} before, "
              f"{still_indexed} after. A suspension is usually temporary and "
              f"must not turn reinstatement into a reindex.")

        print("\n6. Reinstating brings the catalogue back...")
        res = await client.post(f"{SELLER}/{seller_id}/reinstate",
                                json={"reason": "e2e: verification complete"})
        check(res.status_code == 200, "reinstated",
              f"could not reinstate: {res.status_code} {res.text[:150]}")

        restored = await wait_for_visibility(client, seller_id, before_visible,
                                             PROJECTION_WAIT_SECONDS)
        check(restored,
              f"all {before_visible} product(s) are visible again",
              f"only {await visible_count(client, seller_id)} of "
              f"{before_visible} came back")

        print("\n7. A seller the projection has never reached stays visible...")
        # The deploy-safety property. Written as "not explicitly false" rather
        # than "is true", so shipping this could not empty a catalogue that had
        # simply never been projected.
        res = await client.post(f"{ES}/products/_search", json={
            "size": 0, "query": {"bool": {
                "must_not": [{"exists": {"field": "seller_may_sell"}}],
                "filter": [{"bool": {"must_not": [
                    {"term": {"seller_may_sell": False}}]}}]}}})
        unprojected_visible = res.json()["hits"]["total"]["value"]
        res = await client.post(f"{ES}/products/_search", json={
            "size": 0, "query": {"bool": {"must_not": [
                {"exists": {"field": "seller_may_sell"}}]}}})
        unprojected_total = res.json()["hits"]["total"]["value"]
        check(unprojected_visible == unprojected_total,
              f"all {unprojected_total} unprojected document(s) remain "
              f"visible -- unknown is not suspended",
              f"{unprojected_total - unprojected_visible} unprojected "
              f"document(s) were hidden; requiring the flag to be true would "
              f"empty the catalogue on deploy")

    return finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
