import asyncio
import httpx
import sys

from config import describe, new_client, service_url

# Define auxiliary services and their mapped ports from docker-compose.apps.yml
AUX_SERVICES = {name: service_url(name) for name in (
    "user-service",
    "promotion-service",
    "tax-service",
    "media-service",
    "audit-service",
)}

async def check_health(client, name, url):
    print(f"-> Pinging {name} at {url}/health ...")
    try:
        res = await client.get(f"{url}/health", timeout=3.0)
        if res.status_code == 200:
            print(f"   [OK] {name} is ONLINE. Status: 200")
            return True
        elif res.status_code == 404:
            # If they don't have a specific /health endpoint yet but respond to HTTP
            print(f"   [OK] {name} is ONLINE. Status: 404 (Endpoint not found, but service is up)")
            return True
        else:
            print(f"   [WARNING] {name} returned status: {res.status_code}")
            return False
    except Exception as e:
        print(f"   [FAIL] {name} failed to respond: {e}")
        return False

async def main():
    print("==================================================")
    print(" INITIATING AUXILIARY MESH VERIFICATION TEST")
    print("==================================================")
    
    all_passed = True
    async with new_client() as client:
        for name, url in AUX_SERVICES.items():
            success = await check_health(client, name, url)
            if not success:
                all_passed = False
                
    print("\n--- FINAL VERIFICATION ---")
    if all_passed:
        print("[SUCCESS] All auxiliary services are online and responding perfectly!")
        sys.exit(0)
    else:
        print("[FAIL] Some auxiliary services are offline or failing.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
