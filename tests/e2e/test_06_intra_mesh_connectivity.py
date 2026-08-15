import subprocess
import sys

# Define all 20 services with their internal Docker mesh hostnames and ports
INTERNAL_SERVICES = {
    "api-gateway": "http://api-gateway:8000",
    "bff-shop": "http://bff-shop:8001",
    "bff-checkout": "http://bff-checkout:8002",
    "websocket-gateway": "http://websocket-gateway:8003",
    "user-service": "http://user-service:8004",
    "catalog-service": "http://catalog-service:8005",
    "search-service": "http://search-service:8006",
    "cart-service": "http://cart-service:8007",
    "pricing-service": "http://pricing-service:8008",
    "promotion-service": "http://promotion-service:8009",
    "tax-service": "http://tax-service:8010",
    "delivery-quote-service": "http://delivery-quote-service:8011",
    "order-saga": "http://order-saga:8012",
    "inventory-service": "http://inventory-service:8013",
    "fraud-service": "http://fraud-service:8014",
    "payment-service": "http://payment-service:8015",
    "fulfillment-service": "http://fulfillment-service:8016",
    "notification-service": "http://notification-service:8017",
    "media-service": "http://media-service:8018",
    "audit-service": "http://audit-service:8019"
}

def check_internal_connectivity():
    print("==================================================")
    print(" INITIATING INTRA-MESH CONNECTIVITY FORENSIC TEST")
    print("==================================================")
    print("-> Sourcing pings from inside 'api-gateway' container...")
    
    all_passed = True
    
    for name, url in INTERNAL_SERVICES.items():
        # Edge/Node services have different health paths or just return 200/404 on base path
        # Python services all have /health
        if name in ["api-gateway", "bff-shop", "bff-checkout", "websocket-gateway"]:
            target_url = url
        else:
            target_url = f"{url}/health"
            
        print(f"   -> Pinging {target_url} ...")
        # Run wget inside the api-gateway container
        cmd = ["docker", "exec", "ecommerce-platform-api-gateway-1", "wget", "-qO-", "--timeout=2", target_url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                print(f"      [OK] {name} is reachable from inside the mesh.")
            else:
                # Some Node services might return 404 for base url but that still means they are reachable
                if "404" in result.stderr:
                    print(f"      [OK] {name} is reachable (returned 404, but network path is open).")
                else:
                    print(f"      [FAIL] {name} connection failed: {result.stderr.strip()}")
                    all_passed = False
        except Exception as e:
            print(f"      [FAIL] {name} connection error: {e}")
            all_passed = False

    print("\n--- FINAL VERIFICATION ---")
    if all_passed:
        print("[SUCCESS] All 20 nodes are communicating perfectly across the internal mesh!")
        sys.exit(0)
    else:
        print("[FAIL] Some internal mesh connections are failing.")
        sys.exit(1)

if __name__ == "__main__":
    check_internal_connectivity()
