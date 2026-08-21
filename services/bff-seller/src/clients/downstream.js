const { createResilientClient } = require('node-common');
const logger = require('../utils/logger');

// Rule 11: circuit breaker + bulkhead on every synchronous inter-service call,
// attached at the client factory so no call site can skip one.
const TIMEOUT_MS = 3000;

const client = (name, baseURL, bulkhead) =>
  createResilientClient({ name, baseURL, timeout: TIMEOUT_MS, bulkhead, logger });

// Narrower bulkheads than bff-shop. A seller dashboard is a handful of
// operators refreshing a queue, not a storefront under browse traffic, and a
// wide bulkhead here would let one slow seller-service starve the pools that
// buyers depend on.
const sellerClient = client(
  'seller-service', process.env.SELLER_SERVICE_URL || 'http://seller-service:8020', 10);

const orderClient = client(
  'order-saga', process.env.ORDER_SAGA_URL || 'http://order-saga:8012', 10);

const catalogClient = client(
  'catalog-service', process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8005', 10);

const paymentClient = client(
  'payment-service', process.env.PAYMENT_SERVICE_URL || 'http://payment-service:8015', 10);

const fulfillmentClient = client(
  'fulfillment-service',
  process.env.FULFILLMENT_SERVICE_URL || 'http://fulfillment-service:8016', 10);

const allClients = [sellerClient, orderClient, catalogClient, paymentClient,
                    fulfillmentClient];

module.exports = {
  sellerClient,
  orderClient,
  catalogClient,
  paymentClient,
  fulfillmentClient,
  breakerStates: () => allClients.map(c => c.stats())
};
