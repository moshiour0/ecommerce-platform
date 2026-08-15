import sys
import os
import asyncio
from importlib import import_module
from sqlalchemy.ext.asyncio import create_async_engine

# Map of service directory to its local connection string
SERVICES = {
    "catalog-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/catalog_db",
    "user-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/user_db",
    "pricing-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/pricing_db",
    "promotion-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/promotion_db",
    "tax-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/tax_db",
    "cart-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/cart_db",
    "inventory-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/inventory_db",
    "fraud-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/fraud_db",
    "payment-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/payment_ledger_db",
    "order-saga": "postgresql+asyncpg://admin:supersecret@localhost:5432/order_db",
    "delivery-quote-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/delivery_quote_db",
    "fulfillment-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/fulfillment_db",
    "notification-service": "postgresql+asyncpg://admin:supersecret@localhost:5432/notification_db"
}

async def init_service_db(service_name, db_url):
    print(f"[{service_name}] Initializing schemas...")
    
    # 1. Temporarily patch sys.path to resolve the internal "app" module
    service_path = os.path.abspath(os.path.join("services", service_name))
    sys.path.insert(0, service_path)
    
    # 2. Clear previous app modules to prevent cross-service pollution
    for key in list(sys.modules.keys()):
        if key.startswith("app"):
            del sys.modules[key]
            
    try:
        # 3. Dynamically load the SQLAlchemy Base metadata
        models = import_module("app.models")
        
        # 4. Connect to local exposed Postgres port and execute DDL
        engine = create_async_engine(db_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        
        await engine.dispose()
        print(f"[{service_name}] -> SUCCESS: Tables created.")
    except Exception as e:
        print(f"[{service_name}] -> ERROR: {e}")
    finally:
        sys.path.pop(0)

async def main():
    print("--- INITIATING ENTERPRISE SCHEMA DEPLOYMENT ---")
    for service, url in SERVICES.items():
        if os.path.exists(os.path.join("services", service)):
            await init_service_db(service, url)
        else:
            print(f"[{service}] Skipped: Directory not found")
    print("--- DEPLOYMENT COMPLETE ---")

if __name__ == "__main__":
    asyncio.run(main())
