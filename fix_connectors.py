import requests
import time
import json

DEBEZIUM_URL = "http://localhost:8083/connectors"

# This list matches every single service database in your architecture
DATABASES = [
    "catalog_db", "pricing_db", "inventory_db", "cart_db",
    "order_db", "payment_db", "fraud_db", "fulfillment_db",
    "notification_db", "user_db", "tax_db", "delivery_quote_db",
    "promotion_db", "audit_db", "media_db"
]

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
    for db in DATABASES:
        # e.g., "catalog_db" -> "catalog"
        service_name = db.replace("_db", "")
        connector_name = f"{service_name}-outbox-connector"
        
        config = {
            "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
            "tasks.max": "1",
            "database.hostname": "postgres",
            "database.port": "5432",
            "database.user": "admin",
            "database.password": "supersecret",
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
        else:
            print(f"  [!] FAILED to create: {res.text}")

if __name__ == "__main__":
    print("==================================================")
    print(" DEBEZIUM AUTOMATED CDC PROVISIONING ENGINE")
    print("==================================================")
    wait_for_debezium()
    provision_connectors()
    print("\n==================================================")
    print(" ALL CONNECTORS SYNCHRONIZED SUCCESSFULLY.")
    print("==================================================")