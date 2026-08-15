const { createResilientClient } = require('node-common');
const logger = require('../utils/logger');

// Rule 11: circuit breaker + bulkhead on every synchronous inter-service
// call, attached at the client factory so no call site can skip one.
const TIMEOUT_MS = 3000;

const client = (name, baseURL, bulkhead) =>
  createResilientClient({ name, baseURL, timeout: TIMEOUT_MS, bulkhead, logger });

// bff-shop is the read-heavy storefront path, so its bulkheads are wider
// than checkout's — browse traffic is high volume and each call is cheap.
const catalogClient = client(
  'catalog-service', process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8005', 40);

const searchClient = client(
  'search-service', process.env.SEARCH_SERVICE_URL || 'http://search-service:8006', 40);

const cartClient = client(
  'cart-service', process.env.CART_SERVICE_URL || 'http://cart-service:8007', 20);

const allClients = [catalogClient, searchClient, cartClient];

module.exports = {
  catalogClient,
  searchClient,
  cartClient,
  breakerStates: () => allClients.map(c => c.stats())
};
