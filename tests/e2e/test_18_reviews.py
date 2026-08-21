"""
A review is only worth having if the purchase behind it is real.

The unit tests prove the eligibility rules against fake purchases. What they
cannot show is that a real one is actually verified: reviews-service has to
reach order-saga, order-saga has to answer with the buyer and the delivery
state, and the refusal has to happen for a purchase that genuinely exists but
does not qualify. Every one of those links is the sort that has failed silently
in this codebase before.

The assertions worth naming:

**An undelivered order cannot be reviewed.** Not a validation error -- a real
purchase, by the real buyer, refused because the goods have not arrived. This
is the whole verified-purchase claim, and it is the one an attacker probes.

**Somebody else's purchase cannot be reviewed.** The buyer comes from the
gateway's verified token; anything a caller says about who they are is ignored.

**A refused parcel cannot be reviewed.** Under COD the buyer never opened the
box. A return already counts against the seller in the fulfilment metrics, and
letting it also land as a one-star product review would penalise one event
twice.

**The rating reaches ranking.** The point of the whole service: a review has to
turn up in order-saga's quality inputs, or §3g's formula is still scoring on a
number nobody produced.

**Small samples are not shrunk twice.** reviews-service reports the raw mean;
ranking_rules pulls it toward neutral by count. If both shrank, the ranking
would be quietly flatter than either module claims and neither would look
wrong on its own.

Not in CI: it needs order-saga, inventory-service, seller-service, catalog and
reviews-service.
"""

import asyncio
import sys
import uuid

import httpx

from config import connect_with_retry, db_url, describe, new_client, service_url

REVIEWS = service_url("reviews-service")
SAGA = service_url("order-saga") + "/orders"

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
    print("[SUCCESS] Only delivered purchases could be reviewed, only by their "
          "buyer, and the rating reached the ranking formula.")
    return 0


async def a_seller_order(conn, statuses):
    """One real seller order in any of `statuses`, with its buyer and a line."""
    row = await conn.fetchrow(
        f"""
        SELECT so.id, so.status, s.user_id AS buyer_id, so.seller_id,
               (SELECT ol.product_id FROM order_lines ol
                 WHERE ol.seller_order_id = so.id LIMIT 1) AS product_id
        FROM seller_orders so
        JOIN order_saga_states s ON s.id = so.order_id
        WHERE so.status = ANY($1::text[])
          AND EXISTS (SELECT 1 FROM order_lines ol
                       WHERE ol.seller_order_id = so.id)
        ORDER BY so.created_at DESC
        LIMIT 1
        """, list(statuses))
    return row


async def main():
    print("=" * 62)
    print(" REVIEWS: VERIFIED PURCHASE ONLY")
    print("=" * 62)
    print(f"-> {describe()}")

    conn = await connect_with_retry(db_url("order_db"))
    try:
        async with new_client() as client:
            print("\n1. The service states its own policy...")
            res = await client.get(f"{REVIEWS}/policy")
            policy = res.json() if res.status_code == 200 else {}
            check(res.status_code == 200 and policy.get(
                      "verified_purchase_required") is True,
                  f"verified purchase required, {policy.get('min_rating')}-"
                  f"{policy.get('max_rating')} stars, "
                  f"{policy.get('review_window_days')}d window",
                  f"policy endpoint failed: {res.status_code} {res.text[:150]}")

            print("\n2. A real purchase that has not been delivered...")
            pending = await a_seller_order(
                conn, ["PENDING", "INVENTORY_RESERVED", "CONFIRMED",
                       "DISPATCHED"])
            if pending is None:
                check(False, "", "no undelivered seller order to test with")
                return finish()

            res = await client.post(f"{REVIEWS}/reviews", headers={
                "x-user-id": str(pending["buyer_id"])}, json={
                "seller_order_id": str(pending["id"]),
                "product_id": str(pending["product_id"]),
                "product_rating": 5})
            check(res.status_code == 403,
                  f"a {pending['status']} order was refused (403), by its own "
                  f"buyer -- the goods have not arrived",
                  f"an undelivered purchase was not refused: "
                  f"{res.status_code} {res.text[:180]}")

            print("\n3. A delivered purchase, by its buyer...")
            delivered = await a_seller_order(conn, ["DELIVERED", "SETTLED"])
            if delivered is None:
                check(False, "", "no delivered seller order to test with")
                return finish()

            key = str(uuid.uuid4())
            res = await client.post(f"{REVIEWS}/reviews", headers={
                "x-user-id": str(delivered["buyer_id"]),
                "Idempotency-Key": key}, json={
                "seller_order_id": str(delivered["id"]),
                "product_id": str(delivered["product_id"]),
                "product_rating": 5, "seller_rating": 4,
                "title": "Arrived intact", "body": "No complaints."})

            # A purchase reviewed by an earlier run is a 409, which proves the
            # duplicate rule rather than failing this one.
            already = res.status_code == 409
            if already:
                print("      [..]   already reviewed by an earlier run; "
                      "using that review")
                listed = await client.get(
                    f"{REVIEWS}/reviews/products/{delivered['product_id']}")
                mine = [r for r in listed.json()
                        if r["seller_order_id"] == str(delivered["id"])]
                review = mine[0] if mine else None
            else:
                check(res.status_code == 201,
                      f"the buyer's review was accepted ({res.status_code})",
                      f"a delivered purchase was refused: {res.status_code} "
                      f"{res.text[:180]}")
                review = res.json() if res.status_code == 201 else None

            if review is None:
                return finish()

            check(review["product_rating"] == 5 and review["seller_rating"] == 4,
                  "product and seller were rated separately on one submission",
                  f"the two ratings did not survive: {review}")

            print("\n4. The same purchase, claimed by somebody else...")
            res = await client.post(f"{REVIEWS}/reviews", headers={
                "x-user-id": str(uuid.uuid4())}, json={
                "seller_order_id": str(delivered["id"]),
                "product_id": str(delivered["product_id"]),
                "product_rating": 1})
            check(res.status_code == 403,
                  "a stranger could not review someone else's purchase",
                  f"a stranger reviewed another buyer's purchase: "
                  f"{res.status_code} {res.text[:180]}")

            print("\n5. No identity at all...")
            res = await client.post(f"{REVIEWS}/reviews", json={
                "seller_order_id": str(delivered["id"]),
                "product_id": str(delivered["product_id"]),
                "product_rating": 1})
            check(res.status_code == 401,
                  "an unidentified caller was refused (401)",
                  f"a review was accepted with no buyer identity: "
                  f"{res.status_code}")

            print("\n6. Reviewing the same purchase twice...")
            res = await client.post(f"{REVIEWS}/reviews", headers={
                "x-user-id": str(delivered["buyer_id"])}, json={
                "seller_order_id": str(delivered["id"]),
                "product_id": str(delivered["product_id"]),
                "product_rating": 1})
            check(res.status_code == 409,
                  "a second review for one purchase was refused",
                  f"one purchase produced two reviews: {res.status_code}")

            print("\n7. A retry with the same key is not a second review...")
            res = await client.post(f"{REVIEWS}/reviews", headers={
                "x-user-id": str(delivered["buyer_id"]),
                "Idempotency-Key": key}, json={
                "seller_order_id": str(delivered["id"]),
                "product_id": str(delivered["product_id"]),
                "product_rating": 5, "seller_rating": 4})
            if not already:
                check(res.status_code in (200, 201)
                      and res.json().get("id") == review["id"],
                      "the retry returned the original review, not a new one",
                      f"a retried submission created a different review: "
                      f"{res.status_code} {res.text[:180]}")

            print("\n8. A refused parcel cannot be reviewed...")
            returned = await a_seller_order(conn, ["RETURNED", "RTO_IN_TRANSIT"])
            if returned is None:
                print("      [..]   no returned order on this stack to check")
            else:
                res = await client.post(f"{REVIEWS}/reviews", headers={
                    "x-user-id": str(returned["buyer_id"])}, json={
                    "seller_order_id": str(returned["id"]),
                    "product_id": str(returned["product_id"]),
                    "product_rating": 1})
                check(res.status_code == 403,
                      f"a {returned['status']} parcel was refused -- the buyer "
                      f"never opened the box, and the return already counts "
                      f"against the seller in the fulfilment metrics",
                      f"a returned parcel was reviewable: {res.status_code} "
                      f"{res.text[:180]}")

            print("\n9. Ratings outside the scale...")
            # Against a purchase nobody has reviewed yet, or this checks the
            # duplicate guard while claiming to check the rating bounds -- the
            # first version of this test did exactly that and passed on a 409.
            fresh = await conn.fetchrow(
                """
                SELECT so.id, s.user_id AS buyer_id,
                       (SELECT ol.product_id FROM order_lines ol
                         WHERE ol.seller_order_id = so.id LIMIT 1) AS product_id
                FROM seller_orders so
                JOIN order_saga_states s ON s.id = so.order_id
                WHERE so.status IN ('DELIVERED', 'SETTLED')
                  AND so.id <> $1
                  AND EXISTS (SELECT 1 FROM order_lines ol
                               WHERE ol.seller_order_id = so.id)
                ORDER BY so.created_at DESC
                LIMIT 1
                """, delivered["id"])

            if fresh is None:
                print("      [..]   no second delivered order to test bounds "
                      "against without the duplicate rule masking it")
            else:
                for bad in (0, 6):
                    res = await client.post(f"{REVIEWS}/reviews", headers={
                        "x-user-id": str(fresh["buyer_id"])}, json={
                        "seller_order_id": str(fresh["id"]),
                        "product_id": str(fresh["product_id"]),
                        "product_rating": bad})
                    check(res.status_code == 422,
                          f"{bad} stars was rejected as out of scale (422)",
                          f"{bad} stars returned {res.status_code}, not 422 -- "
                          f"if this is 409 the duplicate rule fired first and "
                          f"the bounds were never tested: {res.text[:150]}")

                # And a boundary value is accepted, so "out of scale" is not
                # quietly rejecting everything.
                res = await client.post(f"{REVIEWS}/reviews", headers={
                    "x-user-id": str(fresh["buyer_id"]),
                    "Idempotency-Key": str(uuid.uuid4())}, json={
                    "seller_order_id": str(fresh["id"]),
                    "product_id": str(fresh["product_id"]),
                    "product_rating": 1})
                check(res.status_code in (201, 409),
                      f"1 star -- the bottom of the scale -- is a valid "
                      f"rating ({res.status_code})",
                      f"a valid 1-star rating was rejected: "
                      f"{res.status_code} {res.text[:150]}")

            print("\n10. The rating reaches the ranking formula...")
            seller_id = review["seller_id"]
            res = await client.get(
                f"{REVIEWS}/reviews/sellers/{seller_id}/summary")
            summary = res.json()
            check(res.status_code == 200 and summary["review_count"] >= 1,
                  f"reviews-service reports {summary.get('rating')} from "
                  f"{summary.get('review_count')} review(s)",
                  f"the seller summary is empty: {res.status_code} "
                  f"{res.text[:180]}")

            res = await client.get(f"{SAGA}/seller-metrics",
                                   params={"seller_id": seller_id})
            quality = res.json().get("quality", {})
            check(res.status_code == 200 and quality.get("rating") is not None,
                  f"order-saga's quality inputs carry rating="
                  f"{quality.get('rating')} count={quality.get('review_count')}"
                  f" -- the half of quality_boost that was inert",
                  f"the rating never reached ranking: quality={quality}")

            check(quality.get("rating") == summary.get("rating"),
                  "the rating was passed through unshrunk; ranking_rules does "
                  "the shrinking, once",
                  f"the rating changed in transit: reviews said "
                  f"{summary.get('rating')}, ranking got "
                  f"{quality.get('rating')}. Shrinking in both places pulls a "
                  f"small sample toward neutral twice.")

            print("\n11. An unrated seller is unknown, not bad...")
            res = await client.get(
                f"{REVIEWS}/reviews/sellers/{uuid.uuid4()}/summary")
            unrated = res.json()
            check(res.status_code == 200 and unrated["rating"] is None
                  and unrated["review_count"] == 0,
                  "a seller nobody has rated reports rating=None, so ranking "
                  "can treat them as average rather than bottom",
                  f"an unrated seller did not report unknown: {unrated}")

            print("\n12. The distribution says what the average cannot...")
            res = await client.get(
                f"{REVIEWS}/reviews/products/{delivered['product_id']}/summary")
            product = res.json()
            check(res.status_code == 200
                  and set(product["distribution"].keys()) == {"1", "2", "3",
                                                              "4", "5"},
                  f"every star is reported, average "
                  f"{product.get('average_rating')} over "
                  f"{product.get('review_count')}",
                  f"the product summary is malformed: {product}")
    finally:
        await conn.close()

    return finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
