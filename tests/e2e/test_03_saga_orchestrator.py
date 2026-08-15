import httpx
import uuid
import asyncio
import json
import os
import sys
import asyncpg

ORDER_SAGA_URL = "http://localhost:8012/orders"
STATE_FILE = ".e2e_state.json"
DB_URL = os.getenv("DATABASE_URL", "postgresql://admin:supersecret@localhost:5432/order_db")

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
    
    # Start saga worker in background
    import subprocess
    print("-> Starting saga orchestrator worker...")
    import os
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    worker_proc = subprocess.Popen([sys.executable, "saga_orchestrator_worker.py"], env=env)
    
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
                        
                        if current_status in ["ORDER_COMPLETED", "ROLLBACK_COMPLETED"]:
                            print(f"\n[SUCCESS] SUCCESS: Saga reached terminal state: {current_status}")
                            return
                    else:
                        print(f"   [Attempt {attempt}/15] Failed to fetch order status from DB.")
                print("\n[WARNING] Polling timeout. Saga did not reach terminal state.")
            finally:
                await conn.close()
    finally:
        print("-> Stopping saga orchestrator worker...")
        worker_proc.terminate()
        worker_proc.wait()

if __name__ == "__main__":
    asyncio.run(main())