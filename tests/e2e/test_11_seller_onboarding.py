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

The last section is the one that matters for the rest of the platform. Every
transition must leave exactly one outbox row, because catalog-service will
eventually refuse listings from sellers who may not sell, and it will learn
who those are from these events. A state change that emits nothing is a seller
who is suspended here and still selling everywhere else.

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

    print("\n9. Checking every transition left an event...")
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
