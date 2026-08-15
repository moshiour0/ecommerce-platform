import os
import sys
import requests
import time
import json

# Overridable so the same script provisions a cluster (via port-forward) and
# a compose stack without editing code.
DEBEZIUM_URL = os.getenv("DEBEZIUM_URL", "http://localhost:8083/connectors")

# Rule 8: never hardcode the credential. The compose default is kept only as a
# development fallback; the Kubernetes Postgres uses a generated password from
# the Secret, and every connector failed with "password authentication failed
# for user admin" while this was a literal.
PG_USER = os.getenv("POSTGRES_USER", "admin")
PG_PASSWORD = os.getenv("POSTGRES_PASSWORD", "supersecret")
PG_HOST = os.getenv("POSTGRES_HOST", "postgres")

failures = []

# Maps the real database name (as created by init-dbs.sh) to the service slug
# used for the connector name, topic prefix and replication slot.
#
# Three entries were previously wrong -- payment_db, promotion_db and media_db
# do not exist. Those connectors were provisioned against non-existent
# databases, or fell back to a config posted from the on-disk JSON that has no
# slot.name and therefore collided on the default "debezium" slot.
# Deriving the slug by stripping "_db" is what produced the mismatch, so the
# mapping is now explicit.
DATABASES = {
    "catalog_db":        "catalog",
    "pricing_db":        "pricing",
    "inventory_db":      "inventory",
    "cart_db":           "cart",
    "order_db":          "order",
    "payment_ledger_db": "payment",
    "fraud_db":          "fraud",
    "fulfillment_db":    "fulfillment",
    "notification_db":   "notification",
    "user_db":           "user",
    "tax_db":            "tax",
    "delivery_quote_db": "delivery_quote",
    "promo_db":          "promotion",
    "audit_db":          "audit",
    "media_meta_db":     "media",
}

def wait_for_debezium():
    print("Waiting for Debezium to accept connections...")
    while True:
        try:
            res = requests.get(DEBEZIUM_URL, timeout=3)
            if res.status_code == 200:
                print("-> Debezium is ONLINE.\n")
                break
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)

def provision_connectors():
    for db, service_name in DATABASES.items():
        connector_name = f"{service_name}-outbox-connector"
        
        config = {
            "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
            "tasks.max": "1",
            "database.hostname": PG_HOST,
            "database.port": "5432",
            "database.user": PG_USER,
            "database.password": PG_PASSWORD,
            "database.dbname": db,
            "topic.prefix": f"{service_name}_server",
            "plugin.name": "pgoutput",
            "table.include.list": "public.outbox_messages",
            "slot.name": f"{service_name}_slot",  # <--- THE GUARANTEED UNIQUE SLOT FIX
            "tombstones.on.delete": "false",
            "transforms": "outbox",
            "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
            "transforms.outbox.route.topic.replacement": "${routedByValue}.events",
            "transforms.outbox.table.field.event.id": "id",
            "transforms.outbox.table.field.event.key": "aggregate_id",
            "transforms.outbox.table.field.event.type": "type",
            "transforms.outbox.table.field.event.payload": "payload",
            "transforms.outbox.route.by.field": "aggregate_type",
            "transforms.outbox.table.fields.additional.placement": "created_at:header:timestamp",
            "key.converter": "org.apache.kafka.connect.storage.StringConverter",
            "value.converter": "io.confluent.connect.avro.AvroConverter",
            "value.converter.schema.registry.url": "http://schema-registry:8081"
        }

        payload = {
            "name": connector_name,
            "config": config
        }

        print(f"Provisioning [{connector_name}] with replication slot [{service_name}_slot]...")
        res = requests.post(DEBEZIUM_URL, json=payload)
        
        if res.status_code == 201:
            print(f"  [+] SUCCESS: Connector created.")
        elif res.status_code == 409:
            print(f"  [*] EXISTS: Idempotently updating configuration...")
            put_res = requests.put(f"{DEBEZIUM_URL}/{connector_name}/config", json=config)
            if put_res.status_code in [200, 201]:
                print(f"  [+] SUCCESS: Connector updated.")
            else:
                print(f"  [!] FAILED to update: {put_res.text}")
                failures.append(connector_name)
        else:
            print(f"  [!] FAILED to create: {res.text}")
            failures.append(connector_name)

if __name__ == "__main__":
    print("==================================================")
    print(" DEBEZIUM AUTOMATED CDC PROVISIONING ENGINE")
    print("==================================================")
    wait_for_debezium()
    provision_connectors()
    print("\n==================================================")
    # This previously printed unconditionally. It reported "ALL CONNECTORS
    # SYNCHRONIZED SUCCESSFULLY" after all fifteen failed authentication --
    # the same defect init_schemas.py had, and the reason a broken CDC layer
    # looked healthy for so long.
    if failures:
        print(f" FAILED — {len(failures)} connector(s) not provisioned:")
        for f in failures:
            print(f"   - {f}")
        print("==================================================")
        sys.exit(1)
    print(" ALL CONNECTORS SYNCHRONIZED SUCCESSFULLY.")
    print("==================================================")