import httpx
import uuid
import time
import sys
import asyncio
import asyncpg
import os

# Service URLs
CART_URL = "http://localhost:8007/cart"
BFF_CHECKOUT_URL = "http://localhost:8002/api/checkout"
CATALOG_URL = "http://localhost:8005/products"
PRICING_URL = "http://localhost:8008/prices"
DB_URL = os.getenv("DATABASE_URL", "postgresql://admin:supersecret@localhost:5432/catalog_db")
INVENTORY_DB_URL = os.getenv("INVENTORY_DATABASE_URL", "postgresql://admin:supersecret@localhost:5432/inventory_db")

async def main():
    print("==================================================")
    print(" INITIATING AUTONOMOUS EDGE-TO-SAGA CHECKOUT TEST")
    print("==================================================")
    
    # The saga-dispatcher worker (workers/saga-dispatcher, port 8030) now
    # drives the state machine in the running stack. Spawning a second
    # orchestrator here would double-claim commands, because the old
    # prototype filtered only on type and ignored processed_at.
    print("-> Using the running saga-dispatcher (workers/saga-dispatcher)")
    
    try:
        # --- STEP 0: SEED THE CATALOG & PRICING ---
        print("\n0. Bootstrapping Clean Database State...")
        category_id = str(uuid.uuid4())
        
        try:
            conn = await asyncpg.connect(DB_URL)
            await conn.execute(
                "INSERT INTO categories (id, name, description) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING",
                category_id, "Keyboards", "E2E Testing Phase"
            )
            await conn.close()
        except Exception as e:
            print(f"   [!] Database Setup Failed: {e}")
            sys.exit(1)

        async with httpx.AsyncClient() as client:
            try:
                prod_res = await client.post(
                    f"{CATALOG_URL}/",
                    json={
                        "category_id": category_id,
                        "sku": f"SKU-{uuid.uuid4().hex[:6].upper()}",
                        "name": "Autonomous Saga Keyboard",
                        "description": "Created dynamically by E2E test.",
                        "price_cents": 15000,
                        "is_active": True
                    },
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    timeout=10.0
                )
                
                if prod_res.status_code != 201:
                    print(f"   [!] Failed to create product: {prod_res.text}")
                    sys.exit(1)
                    
                dynamic_product_id = prod_res.json()["id"]
                print(f"   -> Seeded Product ID: {dynamic_product_id}")

                price_res = await client.post(
                    f"{PRICING_URL}/",
                    json={
                        "product_id": dynamic_product_id,
                        "base_price_cents": 15000,
                        "currency": "USD"
                    },
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    timeout=10.0
                )
                print("   -> Seeded Pricing: 15000 cents ($150.00)")

                # Seed inventory explicitly. This test used to rely on
                # inventory-service auto-creating 9999 units for any unknown
                # product — a testing backdoor that made a flash sale
                # unsellout-able and was correctly removed (C-2). A test must
                # arrange its own fixtures, not depend on a production bypass.
                inv_conn = await asyncpg.connect(INVENTORY_DB_URL)
                try:
                    await inv_conn.execute(
                        """INSERT INTO inventory_items
                               (id, product_id, quantity_available, quantity_reserved, updated_at)
                           VALUES ($1, $2, $3, 0, NOW())
                           ON CONFLICT (product_id) DO UPDATE
                               SET quantity_available = EXCLUDED.quantity_available""",
                        uuid.uuid4(), uuid.UUID(dynamic_product_id), 10
                    )
                finally:
                    await inv_conn.close()
                print("   -> Seeded Inventory: 10 units available")

                # --- STEP 1: ADD TO CART ---
                user_id = str(uuid.uuid4())
                print(f"\n1. Adding Keyboard to Cart (User: {user_id[:8]})...")
                cart_res = await client.post(
                    f"{CART_URL}/{user_id}/items",
                    json={"item": {"product_id": dynamic_product_id, "quantity": 1}},
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    timeout=10.0
                )
                print(f"   -> Cart API Status: {cart_res.status_code}")
                # A failed cart write previously printed its status and let the
                # run continue, so a 500 here still ended in a green result.
                if cart_res.status_code >= 400:
                    print(f"   [FAIL] Add-to-cart failed: {cart_res.text[:200]}")
                    sys.exit(1)

                # --- STEP 2: BFF CHECKOUT ---
                print("\n2. Submitting Checkout to BFF (Triggering Synchronous Validations)...")
                checkout_payload = {
                    "telemetry": {"device_id": "test-device-windows", "ip": "127.0.0.1"},
                    "destination_region": "US-East",
                    "weight_grams": 1500
                }

                checkout_res = await client.post(
                    f"{BFF_CHECKOUT_URL}/{user_id}", 
                    json=checkout_payload, 
                    headers={"Idempotency-Key": str(uuid.uuid4())},
                    timeout=15.0
                )
                print(f"   -> BFF Checkout Status: {checkout_res.status_code}")
                
                if checkout_res.status_code == 201:
                    saga_data = checkout_res.json()
                    order_id = saga_data.get('id')
                    print(f"   -> [SUCCESS] BFF accepted cart, passed validations, and initiated Saga.")
                    print(f"   -> Saga Orchestrator ID: {order_id}")
                    print(f"   -> Initial Status: {saga_data.get('status')}")
                    
                    print("\n3. Saga is now executing downstream distributed transactions...")
                    print("   Polling Postgres database directly to verify completion...")
                    db_url = os.getenv("DATABASE_URL", "postgresql://admin:supersecret@localhost:5432/order_db")
                    poll_conn = await asyncpg.connect(db_url)
                    try:
                        for attempt in range(1, 16):
                            await asyncio.sleep(2)
                            row = await poll_conn.fetchrow("SELECT status FROM order_saga_states WHERE id = $1", uuid.UUID(order_id))
                            if row:
                                current_status = row['status']
                                print(f"   [Attempt {attempt}/15] Order Status: {current_status}")
                                # ROLLBACK_COMPLETED is terminal but it is NOT
                                # success for a happy-path test: it means the
                                # order was compensated away. Treating any
                                # terminal state as a pass reported a failed
                                # checkout as green.
                                if current_status == "ORDER_COMPLETED":
                                    print(f"\n[SUCCESS] Saga completed the happy path: {current_status}")
                                    break
                                if current_status == "ROLLBACK_COMPLETED":
                                    print(f"\n[FAIL] Saga compensated instead of completing.")
                                    print("        Rollback itself worked, but this test asserts a")
                                    print("        successful checkout. Check inventory seeding and")
                                    print("        the payment leg.")
                                    sys.exit(1)
                                if current_status == "TIMED_OUT":
                                    print(f"\n[FAIL] Saga was reaped for staleness.")
                                    sys.exit(1)
                            else:
                                print(f"   [Attempt {attempt}/15] Failed to fetch order status from DB.")
                        else:
                            print("\n[FAIL] Polling timeout. Saga did not reach a terminal state.")
                            sys.exit(1)
                    finally:
                        await poll_conn.close()
                        
                else:
                    print(f"   -> [FAILED] BFF rejected checkout: {checkout_res.text}")
                    
            except httpx.RequestError as e:
                print(f"   [!] Connection failed: {e}")
    finally:
        print("-> Done (dispatcher keeps running as a platform service)")

if __name__ == "__main__":
    asyncio.run(main())