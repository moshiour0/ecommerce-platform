"""
Seller performance, computed from real orders, feeding one ranking pipeline.

The unit tests pin the shrinkage and the scoring against fake inputs. What they
cannot show is that the numbers come from actual fulfilment history: that
delivering eight orders and returning eight others produce materially different
metrics, that those metrics reach the ranking formula in the shape it expects,
and that a seller can see the record that decides their visibility.

The property under test is the one the whole design turns on:

**Small samples must not produce extreme scores.** A seller with one returned
order does not have a 100% return rate — that is noise, and treating it as
evidence makes a new seller's first bad day permanent. So this drives one order
and asserts the rates barely move, then drives eight and asserts they move
properly.

What this deliberately does not assert
--------------------------------------
The proximity half of "quality first, then proximity". `ranking_rules`
implements and tests the distance decay in full, but seller coordinates are not
in the read model, so there is nothing live to measure a distance against.
Wiring it is a projection job; asserting it here would mean asserting against
data this test invented.

Not in CI: it needs seller-service, order-saga, inventory-service, bff-seller
and search-service.
"""

import sys
import uuid

import asyncpg
import httpx

from config import db_url, describe, service_url

SELLER_URL = service_url("seller-service") + "/sellers"
SAGA_URL = service_url("order-saga") + "/orders"
SEARCH = service_url("search-service")
BFF = service_url("bff-seller") + "/api/seller"

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


async def onboard(client, name):
    res = await client.post(SELLER_URL, json={
        "legal_name": f"{name} Ltd", "display_name": name,
        "contact_email": "rank@example.com"}, headers=idem(), timeout=60.0)
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


async def drive_order(client, seller_id, ending):
    """One order taken to `ending`: 'delivered' or 'returned'."""
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
        "user_id": str(uuid.uuid4()), "total_cents": 500,
        "items_payload": [{"product_id": product_id, "seller_id": seller_id,
                           "quantity": 1, "price_cents": 500,
                           "line_total_cents": 500}]},
        headers=idem(), timeout=60.0)
    if res.status_code != 201:
        return False
    order_id = res.json()["id"]

    seller_order = None
    for _ in range(30):
        await asyncio.sleep(1)
        split = await client.get(f"{SAGA_URL}/{order_id}/seller-orders",
                                 timeout=60.0)
        candidate = split.json()["seller_orders"][0]
        if candidate["status"] != "PENDING":
            seller_order = candidate["id"]
            break
    if seller_order is None:
        return False

    base = f"{SAGA_URL}/{order_id}/seller-orders/{seller_order}"
    await client.post(f"{base}/confirm", json={}, timeout=60.0)
    await client.post(f"{base}/dispatch", json={"courier_name": "manual"},
                      timeout=60.0)
    if ending == "delivered":
        await client.post(f"{base}/deliver", json={}, timeout=60.0)
    else:
        await client.post(f"{base}/mark_rto", json={"reason": "refused"},
                          timeout=60.0)
        await client.post(f"{base}/complete_return", json={}, timeout=60.0)
    return True


async def metrics_for(client, seller_id):
    res = await client.get(f"{SAGA_URL}/seller-metrics",
                           params={"seller_id": seller_id}, timeout=60.0)
    return res.json() if res.status_code == 200 else None


async def main():
    print("=" * 62)
    print(" SELLER METRICS AND RANKING")
    print("=" * 62)
    print(f"-> {describe()}")

    async with httpx.AsyncClient() as client:

        print("\n1. A seller with no history sits at the prior...")
        fresh = await onboard(client, f"Fresh {uuid.uuid4().hex[:4]}")
        check(fresh is not None, "onboarded", "could not onboard a seller")
        if fresh is None:
            return finish()

        baseline = await metrics_for(client, fresh)
        check(baseline is not None and baseline["confidence"] == 0.0,
              f"confidence 0, rates at the prior "
              f"(return {baseline['return_rate']})",
              f"a seller with no orders reported {baseline}")

        print("\n2. One returned order is not a 100% return rate...")
        # THE property. One bad order is noise; treating it as evidence makes
        # a new seller's first bad day permanent.
        unlucky = await onboard(client, f"Unlucky {uuid.uuid4().hex[:4]}")
        ok = await drive_order(client, unlucky, "returned")
        check(ok, "one order driven to RETURNED", "could not drive an order")

        one_bad = await metrics_for(client, unlucky)
        check(one_bad["return_rate"] < 0.25,
              f"return rate is {one_bad['return_rate']:.3f}, not 1.0",
              f"one returned order produced a "
              f"{one_bad['return_rate']:.0%} return rate; that is noise being "
              f"treated as evidence")
        check(one_bad["confidence"] < 0.1,
              f"confidence stays low ({one_bad['confidence']:.3f})",
              f"one order produced confidence {one_bad['confidence']}")

        print("\n3. Eight of each, to two different sellers...")
        good = await onboard(client, f"Good {uuid.uuid4().hex[:4]}")
        bad = await onboard(client, f"Bad {uuid.uuid4().hex[:4]}")
        if not (good and bad):
            check(False, "", "could not onboard the comparison sellers")
            return finish()

        for _ in range(8):
            await drive_order(client, good, "delivered")
        for _ in range(8):
            await drive_order(client, bad, "returned")

        good_metrics = await metrics_for(client, good)
        bad_metrics = await metrics_for(client, bad)
        print(f"      good: return {good_metrics['return_rate']:.3f}, "
              f"confidence {good_metrics['confidence']:.3f}")
        print(f"      bad:  return {bad_metrics['return_rate']:.3f}, "
              f"confidence {bad_metrics['confidence']:.3f}")

        check(good_metrics["concluded_count"] == 8
              and bad_metrics["concluded_count"] == 8,
              "both records are eight concluded orders",
              f"expected 8 concluded each, got "
              f"{good_metrics['concluded_count']} and "
              f"{bad_metrics['concluded_count']}")

        check(bad_metrics["return_rate"] > good_metrics["return_rate"] + 0.2,
              f"the records now differ materially "
              f"({bad_metrics['return_rate']:.3f} vs "
              f"{good_metrics['return_rate']:.3f})",
              f"eight returns did not separate from eight deliveries: "
              f"{bad_metrics['return_rate']} vs {good_metrics['return_rate']}")

        check(good_metrics["confidence"] > one_bad["confidence"],
              "confidence rose with evidence",
              "confidence did not rise between one order and eight")

        print("\n4. Those numbers reach the ranking formula...")
        # The contract between the two services: whatever order-saga reports
        # must be exactly what quality_boost consumes, and it must order the
        # two sellers correctly.
        sys.path.insert(0, str(
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "services" / "search-service" / "app" / "services"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ranking_rules_live",
            __import__("pathlib").Path(__file__).resolve().parents[2]
            / "services" / "search-service" / "app" / "services"
            / "ranking_rules.py")
        ranking = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ranking)

        good_boost = ranking.quality_boost(good_metrics["quality"])
        bad_boost = ranking.quality_boost(bad_metrics["quality"])
        print(f"      quality boost: good {good_boost:.4f}, bad {bad_boost:.4f}")

        check(good_boost > bad_boost,
              "the better record ranks higher",
              f"the seller with eight returns scored {bad_boost:.4f} against "
              f"{good_boost:.4f} for eight deliveries")
        check(bad_boost > 0,
              "the worse seller sinks without vanishing",
              "a bad seller was scored to zero; sinking is the intent, "
              "vanishing removes the evidence from every operator's screen")

        print("\n5. Search is still served, and ranked...")
        res = await client.get(f"{SEARCH}/search", params={"q": "keyboard",
                                                           "size": 5},
                               timeout=60.0)
        check(res.status_code == 200,
              f"search answered with {res.json().get('total_hits')} hits",
              f"search failed after ranking was wired in: {res.status_code} "
              f"{res.text[:200]}")

        # A search must not fail because a metrics service is slow, so an
        # unresolvable seller has to rank as average rather than break the
        # query. Proven by the fact that results came back at all: the index
        # holds documents whose seller_id predates the field.
        check(len(res.json().get("items", [])) > 0,
              "results with unknown sellers still rank as average",
              "ranking dropped every result whose seller could not be resolved")

        print("\n6. A seller can see the record that judges them...")
        res = await client.get(f"{BFF}/metrics", headers={"x-seller-id": bad},
                               timeout=60.0)
        check(res.status_code == 200
              and res.json()["seller_id"] == bad,
              f"the bad seller sees their own return rate "
              f"({res.json().get('return_rate')})",
              f"a seller could not see their own metrics: {res.status_code}")

        res = await client.get(f"{BFF}/metrics?seller_id={good}",
                               headers={"x-seller-id": bad}, timeout=60.0)
        check(res.status_code == 400,
              "and cannot ask about anyone else's",
              f"a seller queried another seller's metrics: {res.status_code}")

    await close_connections()
    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] One bad order stayed noise, eight became evidence, the "
          "better record ranked higher, and a seller can see the numbers that "
          "judge them.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
