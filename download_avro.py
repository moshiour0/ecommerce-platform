import requests
import os

url = "https://api.hub.confluent.io/api/plugins/confluentinc/kafka-connect-avro-converter/versions/7.5.0/archive"
output_path = os.path.join("docker", "avro-converter.zip")

print("Bypassing Docker network... Downloading Avro Converter using robust streaming...")

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

try:
    # stream=True forces the network to hold the connection open and download in reliable 8KB chunks
    with requests.get(url, headers=headers, stream=True) as response:
        response.raise_for_status()
        with open(output_path, 'wb') as out_file:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    out_file.write(chunk)
                    
    print(f"SUCCESS: File saved securely to {output_path}")
    print(f"File size verified: {os.path.getsize(output_path) / (1024*1024):.2f} MB")
except Exception as e:
    print(f"CRITICAL FAILURE: {e}")