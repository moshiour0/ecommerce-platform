const axios = require('axios');

const TIMEOUT_MS = 3000;

const catalogClient = axios.create({
  baseURL: process.env.CATALOG_SERVICE_URL || 'http://catalog-service:8005',
  timeout: TIMEOUT_MS
});

const searchClient = axios.create({
  baseURL: process.env.SEARCH_SERVICE_URL || 'http://search-service:8006',
  timeout: TIMEOUT_MS
});

const cartClient = axios.create({
  baseURL: process.env.CART_SERVICE_URL || 'http://cart-service:8007',
  timeout: TIMEOUT_MS
});

module.exports = {
  catalogClient,
  searchClient,
  cartClient
};
