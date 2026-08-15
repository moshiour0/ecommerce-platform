import asyncio
import asyncpg

async def restore():
    print("--- RESTORING MISSING DATABASES ---")
    sys_conn = await asyncpg.connect('postgresql://admin:supersecret@localhost:5432/postgres')
    
    for db in ['cart_db', 'promotion_db']:
        try:
            await sys_conn.execute(f'CREATE DATABASE {db}')
            print(f"SUCCESS: {db} restored.")
        except asyncpg.exceptions.DuplicateDatabaseError:
            print(f"OK: {db} already exists.")
        except Exception as e:
            print(f"ERROR: {e}")
            
    await sys_conn.close()

if __name__ == "__main__":
    asyncio.run(restore())