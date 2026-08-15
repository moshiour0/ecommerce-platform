import asyncio
import asyncpg
import httpx
import json
import os
import sys

# Redirect all stdout/stderr to a log file for debugging
# log_f = open("worker_debug.log", "a", encoding="utf-8")
# sys.stdout = log_f
# sys.stderr = log_f


DB_URL = os.getenv("DATABASE_URL", "postgresql://admin:supersecret@localhost:5432/order_db")
INVENTORY_URL = "http://localhost:8013/inventory/reserve"
PAYMENT_URL = "http://localhost:8015/payments/charge"
REFUND_URL = "http://localhost:8015/payments/refund"
SAGA_API_URL = "http://localhost:8012/orders"

# ==========================================
#  CHAOS MODE TOGGLE
# Change to True to simulate a declined credit card
# ==========================================
CHAOS_MODE = False 

async def process_outbox():
    print(f"Saga Worker Online. [CHAOS_MODE: {'ACTIVE ' if CHAOS_MODE else 'DISABLED '}]")
    conn = await asyncpg.connect(DB_URL)
    
    # Use httpx.AsyncClient for non-blocking HTTP requests
    async with httpx.AsyncClient() as client:
        while True:
            # Fetch unprocessed commands WITH concurrency locks
            async with conn.transaction():
                messages = await conn.fetch("""
                    SELECT id, type, payload FROM outbox_messages 
                    WHERE aggregate_type = 'OrderSaga' AND type LIKE '%Command'
                    FOR UPDATE SKIP LOCKED
                """)
                
                for msg in messages:
                    msg_id = msg['id']
                    msg_type = msg['type']
                    payload = json.loads(msg['payload']) if isinstance(msg['payload'], str) else msg['payload']
                    order_id = payload.get("order_id")
                    
                    print(f"\n[Saga Worker] Picked up: {msg_type} for Order: {order_id}")
                    headers = {"Idempotency-Key": f"{msg_id}"}
                    
                    if msg_type == "ReserveInventoryCommand":
                        items = payload.get("items_payload", [])
                        if isinstance(items, dict) and "items" in items: items = items["items"]
                        target_item = items[0] if len(items) > 0 else {}
                        
                        inv_payload = {
                            "product_id": target_item.get("product_id"),
                            "quantity": target_item.get("quantity", 1)
                        }
                        
                        res = await client.post(INVENTORY_URL, json=inv_payload, headers=headers)
                        print(f"  -> Inventory API: {res.status_code}")
                        
                        if res.status_code == 200:
                            await client.post(f"{SAGA_API_URL}/{order_id}/events",
                                        json={"event_type": "InventoryReserved", "payload": {}},
                                        headers={"Idempotency-Key": f"inv-res-{msg_id}"})
                            print("  -> Saga State Advanced: INVENTORY_RESERVED")
                        else:
                            # Without this branch a rejected reservation was dropped
                            # entirely: the saga was never notified and sat at PENDING
                            # until the reaper swept it 15 minutes later. In a flash
                            # sale that is every oversubscribed order.
                            await client.post(f"{SAGA_API_URL}/{order_id}/events",
                                        json={"event_type": "InventoryReservationFailed",
                                              "payload": {"reason": "InsufficientStock",
                                                          "status_code": res.status_code}},
                                        headers={"Idempotency-Key": f"inv-fail-{msg_id}"})
                            print(f"  -> Saga State Reversed: INVENTORY_RESERVATION_FAILED ({res.status_code})")

                    elif msg_type == "ChargePaymentCommand":
                        if CHAOS_MODE:
                            print("  -> [CHAOS] Simulating Credit Card Decline (402 Payment Required)")
                            res_status = 402
                        else:
                            res = await client.post(PAYMENT_URL, json=payload, headers=headers)
                            res_status = res.status_code
                            print(f"  -> Payment API: {res_status}")
                        
                        if res_status == 201:
                            await client.post(f"{SAGA_API_URL}/{order_id}/events", 
                                        json={"event_type": "PaymentCharged", "payload": {}}, 
                                        headers={"Idempotency-Key": f"pay-chg-{msg_id}"})
                            print("  -> Saga State Advanced: PAID")
                        else:
                            # Notify the Orchestrator that the payment failed!
                            await client.post(f"{SAGA_API_URL}/{order_id}/events", 
                                        json={"event_type": "PaymentFailed", "payload": {"reason": "Card Declined"}}, 
                                        headers={"Idempotency-Key": f"pay-fail-{msg_id}"})
                            print("  -> Saga State Reversed: PAYMENT_FAILED (Triggering Rollback...)")

                    elif msg_type in ["ReleaseInventoryCommand", "CompensateInventoryCommand"]:
                        # The Orchestrator's Rollback Response
                        print("  -> COMPENSATING TRANSACTION: Releasing locked inventory back to the warehouse...")
                        await client.post(f"{SAGA_API_URL}/{order_id}/events", 
                                    json={"event_type": "InventoryReleased", "payload": {}}, 
                                    headers={"Idempotency-Key": f"inv-rel-{msg_id}"})
                        print("  -> Saga State Advanced: ROLLBACK_COMPLETED")

                    elif msg_type == "RefundPaymentCommand":
                        # Compensating leg for a charge. Emitted by the reaper when a
                        # saga times out at PAID, or at INVENTORY_RESERVED where it is
                        # unknowable whether the charge landed before the ack dropped.
                        # payment-service treats an uncharged order as a no-op, so this
                        # is safe to issue unconditionally.
                        refund_payload = {
                            "order_id": order_id,
                            "user_id": payload.get("user_id"),
                            "reason": payload.get("reason", "SagaCompensation")
                        }
                        res = await client.post(REFUND_URL, json=refund_payload, headers=headers)
                        print(f"  -> Refund API: {res.status_code} {res.text[:120]}")

                        if res.status_code == 200:
                            await client.post(f"{SAGA_API_URL}/{order_id}/events",
                                        json={"event_type": "PaymentRefunded", "payload": {}},
                                        headers={"Idempotency-Key": f"pay-ref-{msg_id}"})
                            print("  -> Saga State Advanced: ROLLBACK_COMPLETED")

                    elif msg_type == "ConfirmOrderCommand":
                        # The Final Happy Path
                        print("  -> Dispatching to Fulfillment Service (Printing Shipping Label)...")
                        print("  -> Sending Email Receipt via Notification Service...")
                        await client.post(f"{SAGA_API_URL}/{order_id}/events", 
                                    json={"event_type": "OrderCompleted", "payload": {}}, 
                                    headers={"Idempotency-Key": f"ord-cmp-{msg_id}"})
                        print("  -> Saga State Advanced: ORDER_COMPLETED (End of Line)")
                
                    # Mark message as processed if successful
                    await conn.execute("UPDATE outbox_messages SET type = $1 WHERE id = $2", f"{msg_type}_Processed", msg_id)
                        
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(process_outbox())