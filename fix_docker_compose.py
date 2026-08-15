import re

filepath = r"c:\projects\ecommerce-platform\docker-compose.apps.yml"

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

services_to_update = [
    "catalog-service", "search-service", "cart-service", "pricing-service",
    "promotion-service", "tax-service", "delivery-quote-service", "order-saga",
    "inventory-service", "fraud-service", "payment-service", "fulfillment-service",
    "notification-service"
]

# We need to map service names to their specific databases because of the .env file!
# Wait, the .env file does not contain DATABASE_URL variables, it has POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_PORT.
# So the docker-compose needs to construct the DATABASE_URL.
# Example: - DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/user_db

db_mapping = {
    "catalog-service": "catalog_db",
    "search-service": "search_db", # Wait, search service uses elasticsearch, does it have postgres?
    "cart-service": "cart_db",
    "pricing-service": "pricing_db",
    "promotion-service": "promo_db",
    "tax-service": "tax_db",
    "delivery-quote-service": "delivery_quote_db",
    "order-saga": "order_db",
    "inventory-service": "inventory_db",
    "fraud-service": "fraud_db",
    "payment-service": "payment_ledger_db",
    "fulfillment-service": "fulfillment_db",
    "notification-service": "notification_db",
    "user-service": "user_db"
}

# The target docker-compose file doesn't have user-service?! Wait, let's look at the file. user-service is missing in the docker-compose!
# Ah, maybe I should just look for the `ports:` block in each python service and inject the environment right below it.

lines = content.split('\n')
new_lines = []
current_service = None

for i, line in enumerate(lines):
    new_lines.append(line)
    
    match = re.match(r'^  ([a-z-]+):', line)
    if match:
        current_service = match.group(1)
        
    if current_service in db_mapping and line.strip().startswith('ports:'):
        # The next line is the port mapping, e.g., - "8005:8005"
        # We want to inject environment variables after the port mapping.
        pass

    if current_service in db_mapping and line.strip().startswith('command:'):
        # Let's insert environment just BEFORE the command: line.
        db_name = db_mapping[current_service]
        env_str = f"""    environment:
      - DATABASE_URL=postgresql+asyncpg://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}@postgres:5432/{db_name}"""
        
        # Remove the last line (which is command:)
        new_lines.pop()
        new_lines.extend(env_str.split('\n'))
        new_lines.append(line)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write('\n'.join(new_lines))
    print(f"Updated {filepath}")
