#!/bin/bash
set -e
echo "Creating isolated databases for microservices (Rule 2.1)..."
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE user_db;
    CREATE DATABASE catalog_db;
    CREATE DATABASE pricing_db;
    CREATE DATABASE promo_db;
    CREATE DATABASE tax_db;
    CREATE DATABASE delivery_quote_db;
    CREATE DATABASE order_db;
    CREATE DATABASE inventory_db;
    CREATE DATABASE cart_db;
    CREATE DATABASE fraud_db;
    CREATE DATABASE payment_ledger_db;
    CREATE DATABASE fulfillment_db;
    CREATE DATABASE notification_db;
    CREATE DATABASE media_meta_db;
    CREATE DATABASE audit_db;
EOSQL
echo "Isolated databases created successfully."
