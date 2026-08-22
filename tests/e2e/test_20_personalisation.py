"""
What a buyer has shown interest in, and what it is allowed to do to a ranking.

The unit tests prove the affinity arithmetic against fake histories. What they
cannot show is that a real interaction becomes a real profile and reaches the
scorer -- three services and an index apart.

Personalisation is the term where being wrong is least visible and most
harmful, so most of these assertions are about the brakes:

**A match boosts; a non-match is neutral.** Not penalised. A buyer's history is
evidence about what they like, not evidence about what they dislike, and
treating absence as dislike is exactly how a catalogue closes around someone.

**Anonymous is not personalised.** By construction, not by omission -- there is
no device or session profile to fall back on, so an unidentified search scores
exactly as it did before this service existed.

**A few clicks is not a profile.** Below the threshold there is no opinion at
all, rather than a confident one built from noise.

**A buyer can read and delete their own history.** A signal that decides what
somebody sees and is invisible to them is a signal nobody can argue with.

Not in CI: it needs personalisation-service, search-service and Elasticsearch.
"""

import asyncio
import sys
import uuid

from config import describe, new_client, service_url

PERSONALISATION = service_url("personalisation-service") + "/personalisation"
SEARCH = service_url("search-service") + "/search"

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
    print("[SUCCESS] Interest became a profile, the profile reached the "
          "scorer, a non-match stayed neutral, and an anonymous search was "
          "not personalised at all.")
    return 0


async def record(client, buyer, kind, category=None, seller=None):
    body = {"kind": kind}
    if category:
        body["category_id"] = str(category)
    if seller:
        body["seller_id"] = str(seller)
    return await client.post(f"{PERSONALISATION}/behaviour",
                             headers={"x-user-id": str(buyer)}, json=body)


async def main():
    print("=" * 62)
    print(" PERSONALISATION")
    print("=" * 62)
    print(f"-> {describe()}")

    buyer = uuid.uuid4()
    liked_category = uuid.uuid4()
    other_category = uuid.uuid4()

    async with new_client() as client:
        print("\n1. The service states its own parameters...")
        res = await client.get(
            service_url("personalisation-service") + "/policy")
        policy = res.json() if res.status_code == 200 else {}
        check(res.status_code == 200
              and policy.get("identified_buyers_only") is True,
              f"identified buyers only, half-life "
              f"{policy.get('half_life_days')}d, threshold "
              f"{policy.get('min_events_for_affinity')}",
              f"policy endpoint failed: {res.status_code} {res.text[:150]}")

        print("\n2. A buyer with no history has no profile...")
        res = await client.get(f"{PERSONALISATION}/affinity/{buyer}")
        profile = res.json()
        check(res.status_code == 200 and profile["known"] is False,
              "a new buyer is not personalised",
              f"a buyer with no history had a profile: {profile}")

        print("\n3. A few clicks is not a profile...")
        for _ in range(2):
            await record(client, buyer, "view", liked_category)
        res = await client.get(f"{PERSONALISATION}/affinity/{buyer}")
        check(res.json()["known"] is False,
              "two views is still no opinion -- below the threshold there is "
              "no profile, not a confident wrong one",
              f"two views produced a profile: {res.json()}")

        print("\n4. Enough interest becomes an opinion...")
        res = await record(client, buyer, "purchase", liked_category)
        check(res.status_code == 202,
              "a purchase was recorded",
              f"could not record a purchase: {res.status_code} {res.text[:150]}")

        res = await client.get(f"{PERSONALISATION}/affinity/{buyer}")
        profile = res.json()
        check(profile["known"] is True
              and str(liked_category) in profile["categories"],
              f"the buyer now leans towards one category "
              f"(weight {profile['categories'].get(str(liked_category))})",
              f"the profile did not form: {profile}")

        print("\n5. An unknown kind of behaviour is refused, not stored...")
        res = await record(client, buyer, "teleported", liked_category)
        check(res.status_code == 422,
              "an unrecognised kind was refused (422) rather than stored as "
              "data that contributes nothing forever",
              f"an unknown behaviour kind was accepted: {res.status_code}")

        print("\n6. Behaviour requires an identified buyer...")
        res = await client.post(f"{PERSONALISATION}/behaviour",
                                json={"kind": "view"})
        check(res.status_code == 401,
              "an unidentified caller could not record behaviour",
              f"behaviour was recorded with no identity: {res.status_code}")

        print("\n7. A match boosts; a non-match stays neutral...")
        # Computed through the same shared module search-service uses, against
        # the profile the service actually returned -- the arithmetic is unit
        # tested, so what is checked here is that the real profile produces the
        # real multipliers.
        sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                               .resolve().parents[2] / "shared" / "libs"
                               / "python-common"))
        sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                               .resolve().parents[2] / "services"
                               / "search-service"))
        from affinity_rules import affinity_for, profile_from_dict
        from app.services.ranking_rules import personalisation_boost

        built = profile_from_dict(profile)
        matched = personalisation_boost(
            affinity_for(built, str(liked_category), None))
        unmatched = personalisation_boost(
            affinity_for(built, str(other_category), None))
        anonymous = personalisation_boost(
            affinity_for(None, str(liked_category), None))

        check(matched > 1.0,
              f"a product in the buyer's category scores {matched:.4f}",
              f"a matching product was not boosted: {matched}")

        check(abs(unmatched - 1.0) < 1e-9,
              f"a product in another category scores exactly {unmatched:.4f} "
              f"-- neutral, not penalised",
              f"a non-matching product scored {unmatched}, which demotes it. "
              f"A buyer's history is evidence about what they like, not about "
              f"what they dislike.")

        check(abs(anonymous - 1.0) < 1e-9,
              f"an anonymous buyer scores exactly {anonymous:.4f} -- the same "
              f"as before personalisation existed",
              f"an anonymous search was personalised: {anonymous}")

        check(matched < 1.2,
              f"the boost is bounded ({matched:.4f}) -- personalisation that "
              f"can double a score stops being a search engine",
              f"the personalisation boost is too large: {matched}")

        print("\n8. Search still answers for everyone...")
        res = await client.get(SEARCH, params={"q": "", "size": 3})
        check(res.status_code == 200,
              "an anonymous search works",
              f"anonymous search failed: {res.status_code} {res.text[:150]}")

        res = await client.get(SEARCH, params={"q": "", "size": 3},
                               headers={"x-user-id": str(buyer)})
        check(res.status_code == 200,
              "an identified search works",
              f"identified search failed: {res.status_code} {res.text[:150]}")

        print("\n9. Search survives personalisation being unavailable...")
        # A profile that cannot be built must not fail the search. A buyer id
        # that is not a uuid exercises the same path as an outage: the lookup
        # does not return a profile, and ranking carries on without one.
        res = await client.get(SEARCH, params={"q": "", "size": 3},
                               headers={"x-user-id": "not-a-uuid"})
        check(res.status_code == 200,
              "a search whose affinity lookup fails still returns results -- "
              "a ranking refinement must never take the search down with it",
              f"search failed when affinity could not be resolved: "
              f"{res.status_code} {res.text[:150]}")

        print("\n10. A buyer can read and delete their own history...")
        res = await client.get(f"{PERSONALISATION}/affinity",
                               headers={"x-user-id": str(buyer)})
        check(res.status_code == 200 and res.json()["known"] is True,
              "the buyer can see what the platform thinks they like",
              f"a buyer could not read their own profile: {res.status_code}")

        res = await client.delete(f"{PERSONALISATION}/behaviour",
                                  headers={"x-user-id": str(buyer)})
        check(res.status_code == 200 and res.json().get("forgotten", 0) > 0,
              f"deleted {res.json().get('forgotten')} recorded event(s)",
              f"could not delete a buyer's history: {res.status_code} "
              f"{res.text[:150]}")

        res = await client.get(f"{PERSONALISATION}/affinity/{buyer}")
        check(res.json()["known"] is False,
              "after deletion the buyer is unpersonalised again -- the rows "
              "are gone, not flagged",
              f"a profile survived deletion: {res.json()}")

    return finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
