#!/bin/bash
# Wait for Debezium Kafka Connect to start

CONNECT_HOST="http://localhost:8083"
echo "Waiting for Debezium to start on $CONNECT_HOST..."

while : ; do
    # Check if the /connectors endpoint returns 200
    STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$CONNECT_HOST/connectors")
    if [ "$STATUS_CODE" -eq 200 ]; then
        echo "Debezium Connect is up and running!"
        break
    fi
    echo "Waiting for Debezium Connect... (HTTP $STATUS_CODE)"
    sleep 5
done

# Register all connectors found in the connectors/ directory
for config_file in ./connectors/*.json; do
    [ -e "$config_file" ] || continue
    
    # Extract connector name from JSON file name
    CONNECTOR_NAME=$(basename "$config_file" .json)
    
    echo "Registering or updating connector: $CONNECTOR_NAME from $config_file"

    RESPONSE_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d @"$config_file" "$CONNECT_HOST/connectors")
    
    if [ "$RESPONSE_CODE" -eq 201 ]; then
        echo "Connector $CONNECTOR_NAME created successfully."
    elif [ "$RESPONSE_CODE" -eq 409 ]; then
        echo "Connector $CONNECTOR_NAME already exists. Updating configuration..."
        # To update, we extract the "config" object using python and PUT to /config
        python3 -c "import json; d=json.load(open('$config_file')); print(json.dumps(d['config']))" > /tmp/${CONNECTOR_NAME}_config.json
        
        PUT_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "Content-Type: application/json" -d @"/tmp/${CONNECTOR_NAME}_config.json" "$CONNECT_HOST/connectors/$CONNECTOR_NAME/config")
        
        if [ "$PUT_RESPONSE" -eq 200 ] || [ "$PUT_RESPONSE" -eq 201 ]; then
            echo "Connector $CONNECTOR_NAME updated successfully."
        else
            echo "Failed to update connector $CONNECTOR_NAME. HTTP $PUT_RESPONSE"
            # Output full response for debugging
            curl -s -X PUT -H "Content-Type: application/json" -d @"/tmp/${CONNECTOR_NAME}_config.json" "$CONNECT_HOST/connectors/$CONNECTOR_NAME/config"
        fi
        rm -f /tmp/${CONNECTOR_NAME}_config.json
    else
        echo "Failed to create connector $CONNECTOR_NAME. HTTP $RESPONSE_CODE"
        # Output full response for debugging
        curl -s -X POST -H "Content-Type: application/json" -d @"$config_file" "$CONNECT_HOST/connectors"
    fi
done

echo "All connector registrations completed."
