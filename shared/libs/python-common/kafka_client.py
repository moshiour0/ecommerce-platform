import json
import logging
import time
from typing import Callable, Any
from confluent_kafka import Consumer, Producer, KafkaError, KafkaException
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

logger = logging.getLogger(__name__)

class KafkaAvroConsumer:
    def __init__(self, broker_url: str, schema_registry_url: str, group_id: str, topics: list[str]):
        self.broker_url = broker_url
        self.topics = topics
        
        sr_conf = {'url': schema_registry_url}
        self.schema_registry_client = SchemaRegistryClient(sr_conf)
        self.avro_deserializer = AvroDeserializer(self.schema_registry_client)

        consumer_conf = {
            'bootstrap.servers': self.broker_url,
            'group.id': group_id,
            'auto.offset.reset': 'earliest',
            'enable.auto.commit': False
        }
        self.consumer = Consumer(consumer_conf)
        self.consumer.subscribe(self.topics)

        producer_conf = {
            'bootstrap.servers': self.broker_url
        }
        self.dlq_producer = Producer(producer_conf)

    @staticmethod
    def decode_headers(msg) -> dict:
        """Kafka headers as a plain str->str dict.

        Headers are the only place an outbox event's type can travel. The
        Debezium EventRouter can put extra columns in the message envelope
        instead, but that changes the Avro value schema, and the Schema
        Registry runs FULL_TRANSITIVE (Rule 5) -- attempting it failed the
        connector outright with "Schema being registered is incompatible with
        an earlier schema". A header is outside the value, so it costs no
        schema version.
        """
        out = {}
        for key, value in (msg.headers() or []):
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError:
                    value = None
            out[key] = value
        return out

    def consume(self, process_func: Callable[..., None]):
        logger.info(f"Starting consumer for topics: {self.topics}")
        try:
            while True:
                msg = self.consumer.poll(1.0)
                if msg is None:
                    continue

                if msg.error():
                    # CRITICAL FIX: Burn through missing topic errors instantly without sleeping
                    if msg.error().code() in (KafkaError._PARTITION_EOF, KafkaError.UNKNOWN_TOPIC_OR_PART):
                        continue
                    else:
                        logger.error(f"Consumer error: {msg.error()}")
                        time.sleep(1)
                        continue

                original_topic = msg.topic()
                dlq_topic = f"dlq.{original_topic}"
                raw_value = msg.value()

                try:
                    ctx = SerializationContext(original_topic, MessageField.VALUE)
                    deserialized_value = self.avro_deserializer(raw_value, ctx)
                    # Two arguments now: the value, and the headers that
                    # carry the event type. stream-processor is the only
                    # consumer of this class.
                    process_func(deserialized_value, self.decode_headers(msg))
                    self.consumer.commit(asynchronous=False)
                    logger.debug(f"Successfully processed and committed message from {original_topic}")
                except Exception as e:
                    logger.error(f"Failed to process message from {original_topic}: {e}. Routing to DLQ: {dlq_topic}")
                    try:
                        self._route_to_dlq(dlq_topic, raw_value, str(e))
                        self.consumer.commit(asynchronous=False)
                    except Exception as dlq_err:
                        logger.critical(f"CRITICAL: DLQ write failed AND offset NOT committed. Message will be re-delivered. DLQ error: {dlq_err}")

        except KeyboardInterrupt:
            logger.info("Consumer interrupted by user")
        finally:
            self.consumer.close()
            self.dlq_producer.flush()

    def _route_to_dlq(self, dlq_topic: str, raw_payload: bytes, error_msg: str):
        try:
            headers = [('error', error_msg.encode('utf-8'))]
            self.dlq_producer.produce(
                topic=dlq_topic,
                value=raw_payload,
                headers=headers
            )
            self.dlq_producer.poll(0)
            logger.info(f"Successfully routed message to {dlq_topic}")
        except Exception as e:
            logger.error(f"CRITICAL: Failed to route message to DLQ {dlq_topic}: {e}")