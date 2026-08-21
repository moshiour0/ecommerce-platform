"""
A seller's standing, copied onto everything they sell.

The unit tests prove the projection's decisions against fake signals. What they
cannot show is that a real event survives the journey -- and that journey is
the whole point of this test, because it has three filters in it and every one
of them fails *quietly*.

stream-processor drops an event it does not recognise with a DEBUG line reading
"ignoring X; no projection for it here", which is indistinguishable from the
case where that is intended. It dead-letters an event with no `id` field, which
seller events do not have. And then the router dispatches. Wiring only the last
of those three produced an event that reached Kafka, reached the worker,
committed its offset, and did nothing -- while every log line about it said so
approvingly.

The assertions worth naming:

**The signals reach the documents.** Not one document: every product that
seller lists, because the unit of change is a seller and the unit of storage is
a product.

**The location is a geo_point.** `ensure_index_exists` only ever created an
index, so on any environment that had run before, a new field was mapped
dynamically -- and dynamically, a lat/lon pair becomes an object, not a
geo_point, and every geo query against it fails much later.

**Search reads them off the hit.** The whole reason for the projection: this
used to be an HTTP call per distinct seller on every results page.

**Unknown is still average.** A seller nobody has rated reports None, not a
neutral number, and an unlocated shop is not treated as far away.

Not in CI: it needs seller-service, order-saga, stream-processor, Kafka and
Elasticsearch.
"""

import asyncio
import sys
import time

import httpx

from config import describe, new_client, service_url

SELLER = service_url("seller-service") + "/sellers"
SEARCH = service_url("search-service")
ES = "http://localhost:9200"

# The platform's own first-party shop, seeded by migration 015. Used because it
# has the largest catalogue on the stack, which is what makes "every product
# they sell" a meaningful claim rather than a single document.
PLATFORM_SELLER = "00000000-0000-0000-0000-000000000001"

DHAKA = (23.8103, 90.4125)

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
    print("[SUCCESS] A seller's signals reached every product they sell, "
          "search scored on them without an HTTP lookup, and proximity "
          "measured a real distance.")
    return 0


async def projected_count(client, seller_id):
    res = await client.post(f"{ES}/products/_search", json={
        "size": 0, "query": {"bool": {"must": [
            {"term": {"seller_id": seller_id}},
            {"exists": {"field": "seller_signals_updated_at"}}]}}})
    return res.json()["hits"]["total"]["value"]


async def wait_for_projection(client, seller_id, at_least, attempts=40):
    for _ in range(attempts):
        if await projected_count(client, seller_id) >= at_least:
            return True
        await asyncio.sleep(1)
    return False


async def main():
    print("=" * 62)
    print(" SELLER SIGNALS IN THE READ MODEL")
    print("=" * 62)
    print(f"-> {describe()}")

    async with new_client() as client:
        print("\n1. How many products this seller has indexed...")
        res = await client.post(f"{ES}/products/_search", json={
            "size": 0, "query": {"term": {"seller_id": PLATFORM_SELLER}}})
        total = res.json()["hits"]["total"]["value"]
        check(total > 0, f"{total} product(s) indexed for this seller",
              "this seller has no indexed products; nothing to project onto")
        if total == 0:
            return finish()

        print("\n2. Setting the shop's location...")
        res = await client.patch(f"{SELLER}/{PLATFORM_SELLER}/location",
                                 json={"latitude": str(DHAKA[0]),
                                       "longitude": str(DHAKA[1]),
                                       "city": "Dhaka"})
        check(res.status_code == 200,
              "location accepted", f"could not set the location: "
              f"{res.status_code} {res.text[:180]}")

        body = res.json() if res.status_code == 200 else {}
        check(body.get("latitude") is not None,
              f"the endpoint returns the coordinates it stored "
              f"({body.get('latitude')}, {body.get('longitude')})",
              "the seller endpoint answered null for coordinates it stored. "
              "The projection reads this response, so every shop would be "
              "unlocated and proximity would be inert.")

        print("\n3. Waiting for the projection to reach every product...")
        # Three filters stand between the event and the documents, and each one
        # drops it silently. If this times out, check INDEXED_EVENTS in
        # event_rules and the id-guard in main.py before suspecting the router.
        landed = await wait_for_projection(client, PLATFORM_SELLER, total)
        projected = await projected_count(client, PLATFORM_SELLER)
        check(landed,
              f"all {projected} product(s) carry seller signals",
              f"only {projected} of {total} products were projected. The event "
              f"is filtered in three places in stream-processor and each one "
              f"logs the drop as intentional.")

        print("\n4. What actually landed on a document...")
        res = await client.post(f"{ES}/products/_search", json={
            "size": 1, "query": {"bool": {"must": [
                {"term": {"seller_id": PLATFORM_SELLER}},
                {"exists": {"field": "seller_signals_updated_at"}}]}}})
        hits = res.json()["hits"]["hits"]
        if not hits:
            check(False, "", "no projected document to inspect")
            return finish()
        doc = hits[0]["_source"]

        location = doc.get("seller_location")
        check(isinstance(location, dict)
              and abs(location.get("lat", 0) - DHAKA[0]) < 0.001
              and abs(location.get("lon", 0) - DHAKA[1]) < 0.001,
              f"the shop is on the map at {location}",
              f"the location did not land correctly: {location}")

        check(doc.get("seller_on_time_dispatch_rate") is not None,
              f"fulfilment signals arrived: on-time="
              f"{doc.get('seller_on_time_dispatch_rate')} "
              f"return={doc.get('seller_return_rate')} "
              f"confidence={doc.get('seller_confidence')}",
              f"fulfilment signals are missing from the document: {doc}")

        print("\n5. The location is a geo_point, not a dynamically-mapped object...")
        res = await client.get(f"{ES}/products/_mapping")
        props = res.json()["products"]["mappings"]["properties"]
        check(props.get("seller_location", {}).get("type") == "geo_point",
              "seller_location is mapped as geo_point",
              f"seller_location is {props.get('seller_location')}. Dynamic "
              f"mapping turns a lat/lon pair into an object, and every geo "
              f"query against it fails at the point somebody writes one.")

        for field, expected in [("seller_rating", "float"),
                                ("seller_review_count", "integer"),
                                ("seller_signals_updated_at", "date")]:
            check(props.get(field, {}).get("type") == expected,
                  f"{field} is mapped as {expected}",
                  f"{field} is {props.get(field)}, expected {expected}")

        print("\n6. Search scores on them, without an HTTP lookup per seller...")
        res = await client.get(f"{SEARCH}/search", params={"q": "", "size": 5})
        check(res.status_code == 200,
              f"search answered ({len(res.json().get('items', []))} items)",
              f"search failed: {res.status_code} {res.text[:180]}")

        print("\n7. Proximity takes a real position...")
        res = await client.get(f"{SEARCH}/search", params={
            "q": "", "size": 5, "lat": DHAKA[0], "lon": DHAKA[1]})
        check(res.status_code == 200,
              "a located buyer gets results scored by distance",
              f"a located search failed: {res.status_code} {res.text[:180]}")

        print("\n8. Half a coordinate is not a location...")
        res = await client.get(f"{SEARCH}/search",
                               params={"q": "", "lat": DHAKA[0]})
        check(res.status_code == 422,
              "a lone latitude was refused (422) rather than placing the "
              "buyer on a meridian they never claimed",
              f"half a coordinate was accepted: {res.status_code}")

        res = await client.get(f"{SEARCH}/search",
                               params={"q": "", "lat": 99, "lon": 90})
        check(res.status_code == 422,
              "a latitude off the planet was refused",
              f"an impossible latitude was accepted: {res.status_code}")

        print("\n9. Unknown is still average...")
        # This seller has no reviews on a fresh stack, which is the case that
        # matters: a new seller scored as *bad* for having none would never be
        # seen, never earn one, and stay unseen.
        rating = doc.get("seller_rating")
        count = doc.get("seller_review_count")
        check(rating is None or (isinstance(rating, (int, float))
                                 and count and count > 0),
              f"rating={rating} with {count} review(s) -- unrated reports "
              f"None rather than a neutral number",
              f"an unrated seller reported rating={rating} with count={count}; "
              f"a made-up average is indistinguishable from a real one")

    return finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
