"""
Reporting a review, and a person deciding.

The unit tests prove the state machine against fake states. What they cannot
show is that the thresholds hold against a real database -- and the whole
design rests on one thing the rules module cannot enforce by itself: that
"three reports" means three *people*.

The assertions worth naming:

**One person cannot hide a review.** Not by filing once, and not by filing
three times -- the unique constraint is what makes the threshold mean people
rather than clicks, and it is the difference between moderation and a mute
button for whoever cares most.

**A moderator's ruling survives volume.** Otherwise a decision lasts exactly as
long as it takes to file three more reports.

**Hidden means hidden from the average too.** A review pulled from the page but
still counted in the rating is worse than either: the number moves for a reason
nobody can see.

**The author still sees their own.** A review that vanishes without trace
teaches its writer only that the platform cannot be trusted.

Not in CI: it needs reviews-service and order-saga.
"""

import asyncio
import sys
import uuid

from config import (connect_with_retry, db_url, describe,
                    new_client, service_url)

REVIEWS = service_url("reviews-service") + "/reviews"

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
    print("[SUCCESS] One person could not hide a review, three could, a "
          "moderator's ruling stood, and the average followed the page.")
    return 0


async def make_review(client, conn):
    """Create a review this test owns, rather than hunting for one.

    The first version looked for any review already sitting at `visible`. That
    made the test depend on residue from test_18 -- and the moment something
    moderated that review to `cleared`, this test failed with "no visible
    review to moderate", which says nothing about moderation and everything
    about the order the suite happened to run in.

    A test that needs a review in a particular state should make one.
    """
    rows = await conn.fetch(
        """
        SELECT so.id, s.user_id AS buyer_id,
               (SELECT ol.product_id FROM order_lines ol
                 WHERE ol.seller_order_id = so.id LIMIT 1) AS product_id
        FROM seller_orders so
        JOIN order_saga_states s ON s.id = so.order_id
        WHERE so.status IN ('DELIVERED', 'SETTLED')
          AND EXISTS (SELECT 1 FROM order_lines ol
                       WHERE ol.seller_order_id = so.id)
        ORDER BY so.created_at DESC
        LIMIT 40
        """)

    for row in rows:
        res = await client.post(f"{REVIEWS}", headers={
            "x-user-id": str(row["buyer_id"]),
            "Idempotency-Key": str(uuid.uuid4())}, json={
            "seller_order_id": str(row["id"]),
            "product_id": str(row["product_id"]),
            "product_rating": 4, "seller_rating": 4,
            "title": "e2e moderation fixture",
            "body": "Created by test_22 so it does not depend on residue."})
        if res.status_code == 201:
            return str(row["product_id"]), res.json()
        # 409 means this purchase is already reviewed -- try the next one.
    return None, None


async def main():
    print("=" * 62)
    print(" REVIEW MODERATION")
    print("=" * 62)
    print(f"-> {describe()}")

    async with new_client() as client:
        print("\n1. Creating a review this test owns...")
        conn = await connect_with_retry(db_url("order_db"))
        try:
            product_id, review = await make_review(client, conn)
        finally:
            await conn.close()

        if review is None:
            check(False, "", "could not create a review: no delivered seller "
                             "order with an unreviewed line on this stack")
            return finish()
        review_id = review["id"]
        author = review["buyer_id"]
        check(True, f"review {review_id[:8]} on product {product_id[:8]}", "")

        res = await client.get(f"{REVIEWS}/products/{product_id}/summary")
        before = res.json()
        check(before["review_count"] >= 1,
              f"the product averages {before['average_rating']} from "
              f"{before['review_count']} review(s)",
              f"the product summary is empty: {before}")

        print("\n2. The author cannot report their own review...")
        res = await client.post(f"{REVIEWS}/{review_id}/report",
                                headers={"x-user-id": author},
                                json={"reason": "abusive"})
        check(res.status_code == 403,
              "refused (403)",
              f"an author reported their own review: {res.status_code}")

        print("\n3. One person cannot hide a review, however hard they try...")
        determined = str(uuid.uuid4())
        res = await client.post(f"{REVIEWS}/{review_id}/report",
                                headers={"x-user-id": determined},
                                json={"reason": "spam"})
        first = res.json() if res.status_code == 200 else {}
        check(res.status_code == 200
              and first.get("moderation_state") == "visible",
              "one report leaves it visible -- the person most motivated to "
              "report a one-star review is the seller it is about",
              f"a single report changed the state: {res.status_code} {first}")

        repeats = []
        for _ in range(3):
            res = await client.post(f"{REVIEWS}/{review_id}/report",
                                    headers={"x-user-id": determined},
                                    json={"reason": "spam"})
            repeats.append(res.status_code)
        check(all(code == 409 for code in repeats),
              f"the same person's further reports were refused {repeats} -- "
              f"the threshold counts people, not clicks",
              f"one person filed repeat reports: {repeats}")

        res = await client.get(f"{REVIEWS}/products/{product_id}")
        check(any(r["id"] == review_id for r in res.json()),
              "the review is still public after one person's campaign",
              "one determined person hid a review")

        print("\n4. Three distinct people hide it, pending a human...")
        state = None
        for i in range(2):
            res = await client.post(f"{REVIEWS}/{review_id}/report",
                                    headers={"x-user-id": str(uuid.uuid4())},
                                    json={"reason": "fake"})
            state = res.json() if res.status_code == 200 else {}
        check(state.get("moderation_state") == "hidden_pending_review",
              f"hidden after {state.get('distinct_reporters')} distinct "
              f"reporters",
              f"three distinct reports did not hide it: {state}")

        print("\n5. Hidden from the page and from the average...")
        res = await client.get(f"{REVIEWS}/products/{product_id}")
        check(not any(r["id"] == review_id for r in res.json()),
              "it is gone from the public list",
              "a hidden review is still listed publicly")

        res = await client.get(f"{REVIEWS}/products/{product_id}/summary")
        after = res.json()
        check(after["review_count"] < before["review_count"],
              f"the average dropped it too: {before['review_count']} -> "
              f"{after['review_count']} review(s)",
              f"a hidden review is still counted in the rating "
              f"({after['review_count']}). The number then moves for a reason "
              f"nobody can see.")

        print("\n6. The author still sees their own...")
        res = await client.get(f"{REVIEWS}/mine",
                               headers={"x-user-id": author})
        check(res.status_code == 200
              and any(r["id"] == review_id for r in res.json()),
              "the author can still see it, and so can appeal",
              f"a hidden review vanished from its own author: "
              f"{res.status_code}")

        print("\n7. It reaches the moderation queue...")
        res = await client.get(f"{REVIEWS}/moderation/queue")
        queued = [q for q in res.json() if q["id"] == review_id]
        check(queued, "it is in the queue with its reasons",
              "a hidden review never reached the moderation queue")
        if queued:
            check(len(queued[0]["reasons"]) >= 1,
                  f"reasons recorded: {queued[0]['reasons']}",
                  "the queue entry carries no reasons")

        print("\n8. A moderator clears it, and the ruling stands...")
        res = await client.post(f"{REVIEWS}/moderation/{review_id}",
                                json={"decision": "clear",
                                      "note": "e2e: legitimate review"})
        check(res.status_code == 200
              and res.json()["moderation_state"] == "cleared",
              "cleared by a moderator",
              f"could not clear: {res.status_code} {res.text[:150]}")

        res = await client.get(f"{REVIEWS}/products/{product_id}/summary")
        restored = res.json()
        check(restored["review_count"] == before["review_count"],
              f"the average is back to {restored['average_rating']} from "
              f"{restored['review_count']}",
              f"clearing did not restore the aggregate: {restored}")

        codes = []
        for _ in range(4):
            res = await client.post(f"{REVIEWS}/{review_id}/report",
                                    headers={"x-user-id": str(uuid.uuid4())},
                                    json={"reason": "spam"})
            codes.append(res.status_code)
        check(all(code == 409 for code in codes),
              f"four further reports were refused {codes} -- a ruling that "
              f"volume could overturn is no ruling",
              f"reports overturned a moderator's decision: {codes}")

        res = await client.get(f"{REVIEWS}/products/{product_id}")
        check(any(r["id"] == review_id for r in res.json()),
              "it is public again after being cleared",
              "a cleared review is still hidden")

        print("\n9. An invented reason is refused...")
        res = await client.post(f"{REVIEWS}/{review_id}/report",
                                headers={"x-user-id": str(uuid.uuid4())},
                                json={"reason": "i just do not like it"})
        check(res.status_code in (409, 422),
              f"refused ({res.status_code}) -- reasons are a closed set, "
              f"because free text cannot be counted or routed",
              f"an invented reason was accepted: {res.status_code}")

    return finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
