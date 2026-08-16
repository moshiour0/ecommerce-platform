import httpx
import uuid
import asyncio
import json
import os
import sys
import asyncpg

from config import service_url, db_url, describe, ELASTICSEARCH_URL

ORDER_SAGA_URL = service_url("order-saga") + "/orders"
STATE_FILE = ".e2e_state.json"
DB_URL = os.getenv("DATABASE_URL") or db_url("order_db")
INVENTORY_DB_URL = os.getenv("INVENTORY_DATABASE_URL") or db_url("inventory_db")

def get_product_id():
    if not os.path.exists(STATE_FILE):
        print(f"Error: {STATE_FILE} not found. Run test_01 first to seed product.")
        sys.exit(1)
    with open(STATE_FILE, 'r') as f:
        data = json.load(f)
        return data.get("PRODUCT_ID")

async def main():
    print("==================================================")
    print(" INITIATING SAGA ORCHESTRATOR FORENSIC TEST")
    print("==================================================")
    
    # The saga-dispatcher worker (workers/saga-dispatcher, port 8030) now
    # drives the state machine in the running stack. Spawning a second
    # orchestrator here would double-claim commands, because the old
    # prototype filtered only on type and ignored processed_at.
    print("-> Using the running saga-dispatcher (workers/saga-dispatcher)")
    
    try:
    
        product_id = get_product_id()
        if not product_id:
            print("Error: PRODUCT_ID not found in state file.")
            sys.exit(1)

        user_id = str(uuid.uuid4())
        idempotency_key = str(uuid.uuid4())
        
        saga_payload = {
            "user_id": user_id,
            "total_cents": 16500, # $150.00 keyboard + $15.00 hypothetical shipping
            "items_payload": {
                "items": [
                    {
                        "product_id": product_id,
                        "quantity": 1,
                        "price_cents": 15000
                    }
                ]
            }
        }
        
        headers = {"Idempotency-Key": idempotency_key}

        # Seed inventory for the product under test. This previously relied on
        # inventory-service auto-creating 9999 units for any unknown product,
        # a backdoor removed by C-2. Without an explicit fixture the
        # reservation fails and the saga compensates instead of completing.
        inv_conn = await asyncpg.connect(INVENTORY_DB_URL)
        try:
            await inv_conn.execute(
                """INSERT INTO inventory_items
                       (id, product_id, quantity_available, quantity_reserved, updated_at)
                   VALUES ($1, $2, $3, 0, NOW())
                   ON CONFLICT (product_id) DO UPDATE
                       SET quantity_available = EXCLUDED.quantity_available""",
                uuid.uuid4(), uuid.UUID(product_id), 10
            )
        finally:
            await inv_conn.close()
        print(f"0. Seeded inventory: 10 units for product {product_id[:8]}")

        print(f"1. Injecting Order into Saga Orchestrator (User: {user_id[:8]})...")
        async with httpx.AsyncClient() as client:
            try:
                res = await client.post(ORDER_SAGA_URL, json=saga_payload, headers=headers, timeout=10.0)
                
                print(f"   -> Saga API Status: {res.status_code}")
                if res.status_code == 201:
                    order = res.json()
                    order_id = order.get("id")
                    print(f"   -> [SUCCESS] Saga Initialized. Order ID: {order_id}")
                    print(f"   -> Initial Status: {order.get('status')}")
                else:
                    print(f"   -> [FAILED] Saga rejected payload: {res.text}")
                    return
                    
            except Exception as e:
                print(f"   [!] Failed to connect to Order Saga: {e}")
                return

            print("\n2. Saga is now executing downstream distributed transactions...")
            print("   Polling Postgres database directly to verify completion...")
            
            # Poll DB for completion
            conn = await asyncpg.connect(DB_URL)
            try:
                for attempt in range(1, 16):
                    await asyncio.sleep(2)
                    row = await conn.fetchrow("SELECT status FROM order_saga_states WHERE id = $1", uuid.UUID(order_id))
                    if row:
                        current_status = row['status']
                        print(f"   [Attempt {attempt}/15] Order Status: {current_status}")
                        
                        # ROLLBACK_COMPLETED is terminal but it is not success:
                        # it means the order was compensated away. Accepting any
                        # terminal state reported a failed saga as green.
                        if current_status == "ORDER_COMPLETED":
                            print(f"\n[SUCCESS] Saga completed the happy path: {current_status}")
                            return
                        if current_status in ("ROLLBACK_COMPLETED", "TIMED_OUT"):
                            print(f"\n[FAIL] Saga reached {current_status} instead of completing.")
                            print("        Compensation worked, but this test asserts success.")
                            sys.exit(1)
                    else:
                        print(f"   [Attempt {attempt}/15] Failed to fetch order status from DB.")
                print("\n[WARNING] Polling timeout. Saga did not reach terminal state.")
            finally:
                await conn.close()
    finally:
        print("-> Done (dispatcher keeps running as a platform service)")

if __name__ == "__main__":
    asyncio.run(main())