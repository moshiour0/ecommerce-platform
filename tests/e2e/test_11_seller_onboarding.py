"""
Whether a seller can reach the point of selling without earning it.

The unit tests in tests/unit/test_seller_rules.py pin the transition table
against fake inputs, including a breadth-first search proving no route to
ACTIVE skips review. What they cannot prove is that the service, its database
and its HTTP surface agree with the table -- a route that forgets to call the
rules, a status written directly, a transition whose event never reaches the
outbox.

So this drives the real thing. It spends most of its length trying to cheat:
accepting the contract before review, approving a review nobody started,
submitting half the documents, resubmitting after a ban. Every one of those
has to be refused by the service, not merely by the rules module.

Section 9 is the one that matters for the rest of the platform:
catalog-service must actually refuse a listing from a seller who may not sell.
Without it the whole state machine is bookkeeping -- onboarding deciding a
status nothing consults. It drives a second, fresh seller through the
lifecycle rather than reusing the one above, which is banned by then and would
be refused for the wrong reason.

Section 10 asserts every transition left exactly one outbox row. A state
change that emits nothing is a seller suspended here and still selling
everywhere else.

media-service is optional. seller-service holds KYC documents as opaque media
ids and never calls media-service -- Rule 1, and the reason products.seller_id
has no foreign key either -- so the test asserts the confidentiality of those
documents only when media-service is actually reachable.
"""

import json
import sys
import urllib.error
import urllib.request
import uuid

import asyncpg

from config import db_url, describe, service_url

SELLER_URL = service_url("seller-service") + "/sellers"
MEDIA_URL = service_url("media-service") + "/media"

COMPLETE_DOCS = ("national_id", "trade_licence")

failures = []


def check(condition, ok_message, fail_message):
    print(f"      {'[OK]  ' if condition else '[FAIL]'} "
          f"{ok_message if condition else fail_message}")
    if not condition:
        failures.append(fail_message)


def call(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:                       # noqa: BLE001 - reported, not raised
        return 0, {"detail": str(e)}


def register_document(owner_id, doc_type, filename, content_type):
    """A KYC document in media-service, or None if it is not running."""
    status, body = call(MEDIA_URL, "POST", {
        "owner_id": owner_id, "purpose": "seller_document",
        "filename": filename, "content_type": content_type, "size_bytes": 2048,
    }, {"Idempotency-Key": str(uuid.uuid4())})
    return body if status == 201 else None


async def first_category_id():
    """Any category id from catalog_db, or None if catalog is not in play.

    Products require a category, and creating one here would mean this test
    owned catalog data. Reading an existing id keeps it a reader.
    """
    try:
        conn = await asyncpg.connect(db_url("catalog_db"))
    except Exception:                            # noqa: BLE001 - optional dep
        return None
    try:
        row = await conn.fetchrow("SELECT id FROM categories LIMIT 1")
        return str(row["id"]) if row else None
    finally:
        await conn.close()


async def main():
    print("=" * 62)
    print(" SELLER ONBOARDING")
    print("=" * 62)
    print(f"-> {describe()}")

    print("\n1. Registering a seller...")
    status, seller = call(SELLER_URL, "POST", {
        "legal_name": "E2E Traders Ltd",
        "display_name": f"E2E Traders {uuid.uuid4().hex[:6]}",
        "contact_email": "e2e@example.com",
        "city": "Dhaka", "district": "Dhaka",
    }, {"Idempotency-Key": str(uuid.uuid4())})

    check(status == 201, f"registered (HTTP {status})",
          f"could not register a seller: HTTP {status} "
          f"{str(seller.get('detail'))[:200]}")
    if status != 201:
        return finish()

    seller_id = seller["id"]
    check(seller["status"] == "registered" and not seller["may_list_products"],
          "born unable to list products",
          f"a brand new seller reports status={seller['status']} "
          f"may_list_products={seller['may_list_products']}")
    check(sorted(seller["missing_documents"]) == sorted(COMPLETE_DOCS),
          "both required documents are reported missing",
          f"missing_documents was {seller['missing_documents']}")

    print("\n2. Trying to reach 'active' without earning it...")
    for label, path, body in (
        ("accept the contract now", f"/{seller_id}/contract", {"version": 1}),
        ("start a review with no documents", f"/{seller_id}/review", None),
        ("record an approval", f"/{seller_id}/review/result", {"approved": True}),
        ("reinstate without a suspension", f"/{seller_id}/reinstate", None),
    ):
        code, payload = call(f"{service_url('seller-service')}/sellers{path}",
                             "POST", body)
        check(code == 409,
              f"refused: {label} -> 409",
              f"{label} returned {code} instead of 409: "
              f"{str(payload.get('detail'))[:120]}")

    print("\n3. Submitting an incomplete document set...")
    code, payload = call(f"{SELLER_URL}/{seller_id}/documents", "POST",
                         {"documents": [{"document_type": "national_id",
                                         "media_id": str(uuid.uuid4())}]})
    check(code == 422 and "trade_licence" in str(payload.get("detail", "")),
          "an incomplete set never reaches the review queue",
          f"an incomplete submission returned {code} "
          f"{str(payload.get('detail'))[:120]}")

    print("\n4. Submitting the full set...")
    documents, media_assets = [], {}
    for doc_type, filename, content_type in (
        ("national_id", "nid.jpg", "image/jpeg"),
        ("trade_licence", "licence.pdf", "application/pdf"),
    ):
        asset = register_document(seller_id, doc_type, filename, content_type)
        if asset:
            media_assets[doc_type] = asset
        # An opaque id either way: seller-service does not call media-service.
        documents.append({"document_type": doc_type,
                          "media_id": asset["id"] if asset else str(uuid.uuid4())})

    code, payload = call(f"{SELLER_URL}/{seller_id}/documents", "POST",
                         {"documents": documents})
    check(code == 200 and payload.get("status") == "documents_submitted",
          "queued for review",
          f"submitting a complete set returned {code} "
          f"{str(payload.get('detail'))[:120]}")
    check(not payload.get("may_list_products", True),
          "still cannot list products while queued",
          "a queued seller may list products")

    if media_assets:
        print("\n4b. Checking the KYC documents are confidential...")
        for doc_type, asset in media_assets.items():
            check(asset["publicly_servable"] is False,
                  f"{doc_type} is not publicly servable",
                  f"{doc_type} came back publicly_servable=True")
            code, _ = call(f"{MEDIA_URL}/{asset['id']}/location")
            check(code == 404,
                  f"{doc_type} location refused (404)",
                  f"media-service served a location for a {doc_type}: "
                  f"HTTP {code}. KYC documents must never be publicly "
                  f"addressable, scanned clean or not.")
    else:
        print("\n4b. media-service not reachable; skipping the KYC "
              "confidentiality checks (seller-service does not depend on it)")

    print("\n5. Reviewing...")
    code, payload = call(f"{SELLER_URL}/{seller_id}/review", "POST")
    check(code == 200 and payload.get("status") == "under_review",
          "review started", f"starting the review returned {code}")

    code, payload = call(f"{SELLER_URL}/{seller_id}/review/result", "POST",
                         {"approved": False})
    check(code == 422,
          "a rejection with no reason is refused",
          f"a reasonless rejection returned {code}; a seller cannot act on it")

    code, payload = call(f"{SELLER_URL}/{seller_id}/review/result", "POST",
                         {"approved": False, "reason": "trade licence expired"})
    check(code == 200 and payload.get("status") == "rejected",
          "rejected with a reason the seller can act on",
          f"rejection returned {code}")

    print("\n6. Resubmitting and approving...")
    code, payload = call(f"{SELLER_URL}/{seller_id}/documents", "POST",
                         {"documents": documents})
    check(code == 200 and payload.get("status") == "documents_submitted",
          "a rejected seller may resubmit",
          f"resubmission returned {code}")
    check(not payload.get("status_reason"),
          "the stale rejection reason was cleared",
          f"the seller still shows {payload.get('status_reason')!r} next to a "
          f"fresh submission")

    call(f"{SELLER_URL}/{seller_id}/review", "POST")
    code, payload = call(f"{SELLER_URL}/{seller_id}/review/result", "POST",
                         {"approved": True})
    check(code == 200 and payload.get("status") == "approved",
          "approved", f"approval returned {code}")
    check(not payload.get("may_list_products", True),
          "approval alone does not permit selling",
          "an approved seller may list products without accepting the contract")
    check(payload.get("needs_contract_acceptance") is True,
          "the reason is reported as a pending contract",
          "needs_contract_acceptance was not set on an approved seller")

    print("\n7. Accepting the contract...")
    code, payload = call(f"{SELLER_URL}/{seller_id}/contract", "POST",
                         {"version": 9999})
    check(code == 422,
          "a stale contract version is refused",
          f"accepting version 9999 returned {code}; the seller was shown terms "
          f"that are not in force")

    current = call(service_url("seller-service") + "/contract")[1]
    code, payload = call(f"{SELLER_URL}/{seller_id}/contract", "POST",
                         {"version": current["current_contract_version"]})
    check(code == 200 and payload.get("status") == "active",
          "active", f"accepting the current contract returned {code}")
    check(payload.get("may_list_products") is True,
          "and may finally list products",
          "an active seller on the current contract still may not list")

    code, permission = call(f"{SELLER_URL}/{seller_id}/permission")
    check(code == 200 and permission.get("may_list_products") is True,
          "the permission endpoint agrees",
          f"the permission endpoint disagrees with the seller record: "
          f"{permission}")

    print("\n8. Suspending, reinstating, banning...")
    code, payload = call(f"{SELLER_URL}/{seller_id}/suspend", "POST",
                         {"reason": "late dispatch rate 40%"})
    check(code == 200 and not payload.get("may_list_products", True),
          "a suspended seller cannot list",
          f"suspension returned {code} may_list={payload.get('may_list_products')}")

    code, payload = call(f"{SELLER_URL}/{seller_id}/reinstate", "POST")
    check(code == 200 and payload.get("may_list_products") is True,
          "reinstatement restores selling without a second review",
          f"reinstatement returned {code}")

    code, payload = call(f"{SELLER_URL}/{seller_id}/ban", "POST",
                         {"reason": "counterfeit goods"})
    check(code == 200 and payload.get("status") == "banned",
          "banned", f"ban returned {code}")

    code, payload = call(f"{SELLER_URL}/{seller_id}/documents", "POST",
                         {"documents": documents})
    check(code == 409,
          "a banned seller cannot re-enter onboarding by uploading",
          f"a banned seller resubmitted documents: HTTP {code}")

    code, payload = call(f"{SELLER_URL}/{seller_id}/reinstate", "POST")
    check(code == 409, "a ban cannot be reinstated",
          f"a banned seller was reinstated: HTTP {code}")

    print("\n9. Checking catalog-service obeys the seller's status...")
    # The point of the whole state machine. Without this, onboarding decides a
    # status nothing consults, and a suspended seller keeps listing.
    #
    # A fresh seller is driven through the lifecycle again, because the one
    # above is banned by now and every attempt would be refused for the same
    # reason whatever catalog does -- which would pass without proving
    # anything.
    catalog = service_url("catalog-service")
    category_id = await first_category_id()

    if category_id is None:
        print("      catalog-service not reachable, or no category to file a "
              "product under; skipping the enforcement checks")
    else:
        _, subject = call(SELLER_URL, "POST", {
            "legal_name": "Enforcement Ltd",
            "display_name": f"Enforcement {uuid.uuid4().hex[:6]}",
            "contact_email": "enforce@example.com",
        }, {"Idempotency-Key": str(uuid.uuid4())})
        subject_id = subject["id"]

        def try_listing(name):
            code, body = call(f"{catalog}/products/", "POST", {
                "seller_id": subject_id,
                "sku": f"SKU-{uuid.uuid4().hex[:8].upper()}",
                "name": name, "description": "e2e", "price_cents": 9900,
                "category_id": category_id, "is_active": True,
            }, {"Idempotency-Key": str(uuid.uuid4())})
            return code, str(body.get("detail", ""))

        code, detail = try_listing("registered")
        check(code == 403,
              "a registered seller cannot list (403)",
              f"a registered seller listed a product: HTTP {code} {detail[:120]}")

        call(f"{SELLER_URL}/{subject_id}/documents", "POST",
             {"documents": documents})
        call(f"{SELLER_URL}/{subject_id}/review", "POST")
        call(f"{SELLER_URL}/{subject_id}/review/result", "POST", {"approved": True})

        code, detail = try_listing("approved")
        check(code == 403,
              "an approved seller still cannot list before the contract (403)",
              f"an approved seller listed before accepting the contract: "
              f"HTTP {code} {detail[:120]}")

        call(f"{SELLER_URL}/{subject_id}/contract", "POST",
             {"version": current["current_contract_version"]})
        code, detail = try_listing("active")
        check(code == 201,
              "an active seller can list (201)",
              f"an active seller was refused: HTTP {code} {detail[:200]}")

        call(f"{SELLER_URL}/{subject_id}/suspend", "POST",
             {"reason": "counterfeit report"})
        code, detail = try_listing("suspended")
        check(code == 403,
              "a suspended seller is refused immediately (403)",
              f"a suspended seller listed a product: HTTP {code} {detail[:120]}")

        code, detail = call(f"{catalog}/products/", "POST", {
            "seller_id": str(uuid.uuid4()),
            "sku": f"SKU-{uuid.uuid4().hex[:8].upper()}",
            "name": "ghost", "description": "e2e", "price_cents": 100,
            "category_id": category_id, "is_active": True,
        }, {"Idempotency-Key": str(uuid.uuid4())})
        check(code == 404,
              "an unknown seller is 404, distinct from a refusal",
              f"an unknown seller returned {code}; a typo'd id and a suspended "
              f"shop must not look the same to a client")

    print("\n10. Checking every transition left an event...")
    # Rule 3. catalog-service will learn who may sell from these; a transition
    # that emits nothing is a seller suspended here and still selling
    # everywhere else.
    conn = await asyncpg.connect(db_url("seller_db"))
    try:
        rows = await conn.fetch(
            "SELECT type, payload->>'status' AS status, "
            "       payload->>'may_list_products' AS may_list "
            "FROM outbox_messages WHERE aggregate_id = $1 "
            "ORDER BY created_at", seller_id)
    finally:
        await conn.close()

    emitted = [r["type"] for r in rows]
    print(f"      events: {emitted}")

    expected = [
        "SellerRegistered", "SellerDocumentsSubmitted", "SellerReviewStarted",
        "SellerRejected", "SellerDocumentsSubmitted", "SellerReviewStarted",
        "SellerApproved", "SellerActivated", "SellerSuspended",
        "SellerReinstated", "SellerBanned",
    ]
    check(emitted == expected,
          f"all {len(expected)} transitions emitted exactly one event each",
          f"expected {expected} but the outbox holds {emitted}")

    # The flag consumers will act on has to match the status in the same row.
    wrong = [(r["status"], r["may_list"]) for r in rows
             if (r["may_list"] == "true") != (r["status"] == "active")]
    check(not wrong,
          "every event's may_list_products agrees with its status",
          f"events disagree with themselves: {wrong}")

    return finish()


def finish():
    print("\n--- FINAL VERIFICATION ---")
    if failures:
        print(f"[FAIL] {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("[SUCCESS] A seller reaches 'active' only through review and a "
          "contract, every shortcut is refused, and every transition is on "
          "the outbox.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
