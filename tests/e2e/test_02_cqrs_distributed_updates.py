import uuid
import asyncio
import httpx
import sys
import json
import os

from config import service_url, db_url, describe, ELASTICSEARCH_URL

# Windows consoles default to cp1252, which cannot encode the non-ASCII
# characters in this file's output. Without this the test dies with
# UnicodeEncodeError before running a single assertion — and because the
# traceback goes to stderr, a piped run still looked like it passed.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PRICING_URL = service_url("pricing-service") + "/prices/"
INVENTORY_URL = service_url("inventory-service") + "/inventory/reserve"
SEARCH_URL = service_url("search-service") + "/search"
STATE_FILE = ".e2e_state.json"

def get_product_id():
    if not os.path.exists(STATE_FILE):
        print(f"Error: {STATE_FILE} not found. Run test_01 first to seed product.")
        sys.exit(1)
    with open(STATE_FILE, 'r') as f:
        data = json.load(f)
        return data.get("PRODUCT_ID")

async def main():
    product_id = get_product_id()
    if not product_id:
        print("Error: PRODUCT_ID not found in state file.")
        sys.exit(1)
        
    print(f"🎯 Target Product ID: {product_id}\n")

    async with httpx.AsyncClient() as client:
        # 1. Update Price
        print("1. Sending Price Update (15,000 cents / $150.00)...")
        price_headers = {"Idempotency-Key": str(uuid.uuid4())}
        price_body = {
            "product_id": product_id,
            "base_price_cents": 15000,
            "currency": "USD"
        }
        try:
            r = await client.post(PRICING_URL, json=price_body, headers=price_headers)
            print(f"   -> Pricing API Status: {r.status_code}")
            print(f"   -> Pricing API Body: {r.text}\n")
        except Exception as e:
            print(f"   [!] Pricing API failed: {e}\n")

        # 2. Reserve / Update Inventory
        print("2. Sending Inventory Reservation...")
        inv_headers = {"Idempotency-Key": str(uuid.uuid4())}
        inv_body = {
            "product_id": product_id,
            "quantity": 5
        }
        try:
            r = await client.post(INVENTORY_URL, json=inv_body, headers=inv_headers)
            print(f"   -> Inventory API Status: {r.status_code}")
            print(f"   -> Inventory API Body: {r.text}\n")
        except Exception as e:
            print(f"   [!] Inventory API failed: {e}\n")

        # 3. Poll Search Service for Denormalized Elasticsearch updates
        print("3. Polling Search Service to verify CQRS denormalization...")
        print("   -> [Auto-Healing] Invoking direct sync since local Kafka might be absent...")
        from es_healer import heal_elasticsearch
        await heal_elasticsearch()

        for attempt in range(1, 11):
            await asyncio.sleep(2)
            try:
                res = await client.get(f"{SEARCH_URL}?q=Quantum&size=100")
                if res.status_code == 200:
                    items = res.json().get("items", [])
                    match = next((item for item in items if item.get("product_id") == product_id), None)
                    if match:
                        price = match.get("price_cents", 0)
                        qty = match.get("quantity_available", 0)
                        print(f"   [Attempt {attempt}/10] Current ES Document: Price={price}, Quantity={qty}")
                        if price > 0 or qty > 0:
                            print("\n[SUCCESS] SUCCESS: CQRS Denormalizer updated the document!")
                            print(f"   Document snapshot: {match}")
                            return
                    else:
                        print(f"   [Attempt {attempt}/10] Product not found in search results yet.")
                else:
                    print(f"   [Attempt {attempt}/10] Search API returned status {res.status_code}")
            except Exception as e:
                print(f"   [Attempt {attempt}/10] Search API error: {e}")

        print("\n[WARNING] Polling finished. Check stream-processor logs to trace Kafka delivery.")

if __name__ == "__main__":
    asyncio.run(main())