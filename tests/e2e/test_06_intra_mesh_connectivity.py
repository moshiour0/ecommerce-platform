import subprocess
import sys

from config import describe, internal_url, mesh_exec_prefix, run_probe

# Define all 20 services with their internal Docker mesh hostnames and ports
# Built from the shared port map so this list cannot drift from the platform.
# The addresses are identical under compose and Kubernetes; only the way into
# the mesh differs, which mesh_exec_prefix handles.
INTERNAL_SERVICES = {name: internal_url(name) for name in (
    "api-gateway", "bff-shop", "bff-checkout", "websocket-gateway",
    "user-service", "catalog-service", "search-service", "cart-service",
    "pricing-service", "promotion-service", "tax-service",
    "delivery-quote-service", "order-saga", "inventory-service",
    "fraud-service", "payment-service", "fulfillment-service",
    "notification-service", "media-service", "audit-service",
)}

def check_internal_connectivity():
    print("==================================================")
    print(" INITIATING INTRA-MESH CONNECTIVITY FORENSIC TEST")
    print("==================================================")
    print(f"-> {describe()}")
    print("-> Sourcing pings from inside the api-gateway container...")
    
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
        cmd = mesh_exec_prefix("api-gateway") + ["wget", "-qO-", "--timeout=2", target_url]
        try:
            result = run_probe(cmd)
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
