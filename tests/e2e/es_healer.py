import asyncio
import asyncpg
import httpx
import os

# Connection strings for the three source-of-truth databases
CATALOG_DB = os.getenv("CATALOG_DB_URL", "postgresql://admin:supersecret@localhost:5432/catalog_db")
PRICING_DB = os.getenv("PRICING_DB_URL", "postgresql://admin:supersecret@localhost:5432/pricing_db")
INVENTORY_DB = os.getenv("INVENTORY_DB_URL", "postgresql://admin:supersecret@localhost:5432/inventory_db")
ES_URL = "http://localhost:9200/products/_doc/"

async def heal_elasticsearch():
    print("Starting Elasticsearch Direct Sync Healer...")
    
    # 1. Connect to all three databases
    cat_conn = await asyncpg.connect(CATALOG_DB)
    pri_conn = await asyncpg.connect(PRICING_DB)
    inv_conn = await asyncpg.connect(INVENTORY_DB)
    
    # 2. Fetch all raw products from Catalog
    # In a massive DB, this should be paginated/cursored. For E2E we load them but use bulk lookups.
    products = await cat_conn.fetch("SELECT id, name, description FROM products")
    print(f"Found {len(products)} products in Catalog.")
    
    if not products:
        print("No products to heal.")
        await cat_conn.close()
        await pri_conn.close()
        await inv_conn.close()
        return

    # Extract all product IDs to do a bulk IN query
    product_ids = [p['id'] for p in products]

    # 3. Fetch latest prices in bulk
    price_records = await pri_conn.fetch(
        "SELECT product_id, base_price_cents FROM prices WHERE product_id = ANY($1)", 
        product_ids
    )
    price_map = {str(r['product_id']): r['base_price_cents'] for r in price_records}
    
    # 4. Fetch latest stock in bulk
    inv_records = await inv_conn.fetch(
        "SELECT product_id, quantity_available FROM inventory_items WHERE product_id = ANY($1)", 
        product_ids
    )
    stock_map = {str(r['product_id']): r['quantity_available'] for r in inv_records}
    
    async with httpx.AsyncClient() as client:
        for p in products:
            p_id = str(p['id'])
            
            price = price_map.get(p_id, 0)
            stock = stock_map.get(p_id, 0)
            
            # 5. Denormalize into a single JSON document
            es_doc = {
                "product_id": p_id,
                "name": p['name'],
                "description": p['description'],
                "price_cents": price,
                "quantity_available": stock,
                "is_active": True
            }
            
            # 6. Forcefully inject into Elasticsearch
            try:
                res = await client.put(f"{ES_URL}{p_id}", json=es_doc)
                
                if res.status_code in [200, 201]:
                    print(f"  [OK] Synced: {p['name']} (Price: {price}, Stock: {stock})")
                else:
                    print(f"  [FAIL] Failed to sync {p_id}: {res.text}")
            except httpx.RequestError as exc:
                print(f"  [FAIL] HTTP Exception trying to sync {p_id}: {exc}")

    await cat_conn.close()
    await pri_conn.close()
    await inv_conn.close()
    print("Healing complete. Search API should now have accurate data.")

if __name__ == "__main__":
    asyncio.run(heal_elasticsearch())