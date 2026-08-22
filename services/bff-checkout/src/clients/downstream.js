const { createResilientClient } = require('node-common');
const logger = require('../utils/logger');

// Rule 11: every synchronous inter-service call is wrapped in a circuit
// breaker (open after 5 consecutive failures, 30s open, single half-open
// probe) and a bulkhead. Breakers are attached here at the client factory
// rather than at each call site, so a new route cannot accidentally bypass
// one. Routes keep using `catalogClient.get(...)` exactly as before.
const TIMEOUT_MS = 3000;

const client = (name, baseURL, bulkhead) =>
  createResilientClient({ name, baseURL, timeout: TIMEOUT_MS, bulkhead, logger });

const cartClient = client(
  'cart-service', process.env.CART_SERVICE_URL || 'http://cart-service:8007', 20);

// Checkout validates every line item against catalog and pricing (Rule 7),
// so these two see the highest fan-out and get a larger bulkhead.
const catalogClient = client(
  'catalog-service', process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8005', 40);

const pricingClient = client(
  'pricing-service', process.env.PRICING_SERVICE_URL || 'http://pricing-service:8008', 40);

const fraudClient = client(
  'fraud-service', process.env.FRAUD_SERVICE_URL || 'http://fraud-service:8014', 20);

const deliveryQuoteClient = client(
  'delivery-quote-service', process.env.DELIVERY_QUOTE_SERVICE_URL || 'http://delivery-quote-service:8011', 20);

// Asked once per distinct seller in a cart, not once per line: whether a
// seller may still be sold from is a fact about the seller, and a cart of ten
// items from one shop is one question.
const sellerClient = client(
  'seller-service', process.env.SELLER_SERVICE_URL || 'http://seller-service:8020', 20);

// The saga is the write path. A smaller bulkhead here means a stalled saga
// cannot consume the whole pool and take the read-only quote paths with it.
const orderSagaClient = client(
  'order-saga', process.env.ORDER_SAGA_URL || 'http://order-saga:8012', 10);

const allClients = [
  cartClient, catalogClient, pricingClient,
  fraudClient, deliveryQuoteClient, orderSagaClient, sellerClient
];

module.exports = {
  cartClient,
  catalogClient,
  pricingClient,
  fraudClient,
  deliveryQuoteClient,
  orderSagaClient,
  sellerClient,
  // Surfaced on /health so operators can see which dependencies are tripped.
  breakerStates: () => allClients.map(c => c.stats())
};
