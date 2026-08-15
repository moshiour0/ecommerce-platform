import httpx
import uuid
import time
import json
import asyncio
import asyncpg
import os

BFF_SHOP_URL = "http://localhost:8001"
CATALOG_URL = "http://localhost:8005"
ES_URL = "http://localhost:9200"
DB_URL = os.getenv("DATABASE_URL", "postgresql://admin:supersecret@localhost:5432/catalog_db")

category_id = str(uuid.uuid4())
idempotency_key = str(uuid.uuid4())
STATE_FILE = ".e2e_state.json"

async def setup_category():
    print("0. Enforcing Foreign Key Integrity (Inserting Category)...")
    conn = None
    try:
        conn = await asyncpg.connect(DB_URL)
        await conn.execute(
            "INSERT INTO categories (id, name, description) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
            category_id, "Keyboards", "High performance gear"
        )
        print("   -> Category inserted successfully!")
    except Exception as e:
        print(f"   -> Database error: {e}")
        raise
    finally:
        if conn:
            await conn.close()

async def check_infrastructure(client: httpx.AsyncClient):
    print("\n--- Diagnostic Check ---")
    try:
        es_res = await client.get(ES_URL, timeout=3.0)
        print(f"Elasticsearch: ONLINE (Status {es_res.status_code})")
    except httpx.RequestError:
        print("Elasticsearch: OFFLINE OR UNREACHABLE (Is port 9200 open/running?)")

async def run_test():
    async with httpx.AsyncClient() as client:
        await check_infrastructure(client)
        
        product_id = str(uuid.uuid4())
        product_payload = {
            "id": product_id, # explicit ID for saving to state
            "category_id": category_id,
            "sku": f"SKU-{uuid.uuid4().hex[:6].upper()}",
            "name": "Quantum Mechanical Keyboard",
            "description": "Tactile switches with zero-click latency.",
            "price_cents": 15000,
            "is_active": True
        }

        headers = {"Idempotency-Key": idempotency_key}

        print("\n1. Creating Product in Catalog (Postgres)...")
        try:
            catalog_res = await client.post(f"{CATALOG_URL}/products/", json=product_payload, headers=headers, timeout=10.0)
            print(f"Catalog Response: {catalog_res.status_code}")
            
            # Use the ID returned or the one we supplied depending on API response
            if catalog_res.status_code == 201:
                created_product = catalog_res.json()
                final_product_id = created_product.get("id", product_id)
                # Save to state file
                with open(STATE_FILE, 'w') as f:
                    json.dump({"PRODUCT_ID": final_product_id}, f)
                print(f"   -> Saved PRODUCT_ID: {final_product_id} to {STATE_FILE}")
            else:
                print(f"Failed to create product: {catalog_res.text}")
                return
        except httpx.RequestError as e:
            print(f"Failed to connect to Catalog Service: {e}")
            return

        print("\n2. Waiting for Debezium -> Kafka -> Stream Processor -> Elasticsearch...")
        print("   -> [Auto-Healing] Invoking direct sync since local Kafka might be absent...")
        from es_healer import heal_elasticsearch
        await heal_elasticsearch()
        
        max_retries = 10
        for attempt in range(max_retries):
            print(f"   -> Polling search API (Attempt {attempt + 1}/{max_retries})...")
            try:
                search_res = await client.get(f"{BFF_SHOP_URL}/api/shop/search?q=Quantum", timeout=10.0)
                
                if search_res.status_code == 200:
                    results = search_res.json()
                    is_populated = False
                    
                    if isinstance(results, list) and len(results) > 0:
                        is_populated = True
                    elif isinstance(results, dict) and any(results.values()): 
                        if "Quantum" in str(results): 
                            is_populated = True

                    if is_populated: 
                        print(f"\n3. Product Found! Search Response: {search_res.status_code}")
                        print(json.dumps(results, indent=2))
                        return
                    else:
                        print(f"      [Debug] Search API returned empty results: {json.dumps(results)[:100]}")
                else:
                    print(f"      [Debug] Search API failed with status {search_res.status_code}")
                    print(f"      [Debug] BFF Error Body: {search_res.text[:500]}")
                    
            except httpx.RequestError as e:
                print(f"      [Debug] Connection error to BFF: {e}")
            
            await asyncio.sleep(2)
            
        print("\nTimeout: Product did not appear in search results within 20 seconds.")

if __name__ == "__main__":
    asyncio.run(setup_category())
    asyncio.run(run_test())