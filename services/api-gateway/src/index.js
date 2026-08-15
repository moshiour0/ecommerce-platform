require('dotenv').config();
const express = require('express');
const cors = require('cors');
const { createProxyMiddleware } = require('http-proxy-middleware');
const logger = require('./utils/logger');
const limiter = require('./middleware/ratelimit');
const verifyToken = require('./middleware/auth');

const app = express();
const PORT = process.env.PORT || 8000;

const BFF_SHOP_URL = process.env.BFF_SHOP_URL || 'http://bff-shop:8001';
const BFF_CHECKOUT_URL = process.env.BFF_CHECKOUT_URL || 'http://bff-checkout:8002';

// 1. Basic Middleware
app.use(cors());

// 2. Global Rate Limiting
app.use(limiter);

// 3. Request Logging
app.use((req, res, next) => {
  logger.info(`Incoming Request: ${req.method} ${req.originalUrl} from ${req.ip}`);
  next();
});

// 4. Health Check (Public)
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

// 5. Authentication Verification (Applied before proxying)
app.use('/api', verifyToken);

// 6. Proxy Configuration

// Proxy /api/shop to bff-shop
app.use('/api/shop', createProxyMiddleware({
  target: BFF_SHOP_URL,
  changeOrigin: true,
  pathRewrite: {
    '^/api/shop': '/api/shop' // keep the base path
  },
  onProxyReq: (proxyReq, req, res) => {
    // If auth middleware decoded a user, we can pass it securely
    if (req.user) {
      proxyReq.setHeader('x-user-id', req.user.sub || req.user.user_id || 'unknown');
    }
  },
  onError: (err, req, res) => {
    logger.error(`Proxy error to bff-shop: ${err.message}`);
    res.status(502).json({ detail: 'Bad Gateway' });
  }
}));

// Proxy /api/checkout to bff-checkout
app.use('/api/checkout', createProxyMiddleware({
  target: BFF_CHECKOUT_URL,
  changeOrigin: true,
  pathRewrite: {
    '^/api/checkout': '/api/checkout'
  },
  onProxyReq: (proxyReq, req, res) => {
    if (req.user) {
      proxyReq.setHeader('x-user-id', req.user.sub || req.user.user_id || 'unknown');
    }
  },
  onError: (err, req, res) => {
    logger.error(`Proxy error to bff-checkout: ${err.message}`);
    res.status(502).json({ detail: 'Bad Gateway' });
  }
}));

// Fallback for unmatched routes
app.use((req, res) => {
  res.status(404).json({ detail: 'Not Found' });
});

app.listen(PORT, () => {
  logger.info(`API Gateway listening on port ${PORT}`);
});
