const axios = require('axios');

const TIMEOUT_MS = 3000;

const cartClient = axios.create({
  baseURL: process.env.CART_SERVICE_URL || 'http://cart-service:8007',
  timeout: TIMEOUT_MS
});

const catalogClient = axios.create({
  baseURL: process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8005',
  timeout: TIMEOUT_MS
});

const pricingClient = axios.create({
  baseURL: process.env.PRICING_SERVICE_URL || 'http://pricing-service:8008',
  timeout: TIMEOUT_MS
});

const fraudClient = axios.create({
  baseURL: process.env.FRAUD_SERVICE_URL || 'http://fraud-service:8014',
  timeout: TIMEOUT_MS
});

const deliveryQuoteClient = axios.create({
  baseURL: process.env.DELIVERY_QUOTE_SERVICE_URL || 'http://delivery-quote-service:8011',
  timeout: TIMEOUT_MS
});

const orderSagaClient = axios.create({
  baseURL: process.env.ORDER_SAGA_URL || 'http://order-saga:8012',
  timeout: TIMEOUT_MS
});

module.exports = {
  cartClient,
  catalogClient,
  pricingClient,
  fraudClient,
  deliveryQuoteClient,
  orderSagaClient
};
