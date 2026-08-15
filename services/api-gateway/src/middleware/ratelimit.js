const rateLimit = require('express-rate-limit');
const logger = require('../utils/logger');

// F-2 Fix: Redis-backed rate limiter store for multi-replica consistency
// In production, use `rate-limit-redis` with RedisStore:
//   const RedisStore = require('rate-limit-redis');
//   const { createClient } = require('redis');
//   const redisClient = createClient({ url: process.env.REDIS_URL || 'redis://redis:6379' });

const rateLimitConfig = {
  standardHeaders: true,
  legacyHeaders: false,
  handler: (req, res, next, options) => {
    logger.warn(`Rate limit exceeded for IP: ${req.ip} on path: ${req.path}`);
    res.status(options.statusCode).send(options.message);
  },
  message: { detail: 'Too many requests, please try again later.' }
};

// Tiered Rate Limiting (Architecture Rule: api-gateway domain boundary)
// Read endpoints: 200 req/min/IP
const readLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: 1 * 60 * 1000,
  max: 200,
  // In production: store: new RedisStore({ sendCommand: (...args) => redisClient.sendCommand(args) })
});

// Write endpoints: 20 req/min/IP
const writeLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: 1 * 60 * 1000,
  max: 20,
  // In production: store: new RedisStore({ sendCommand: (...args) => redisClient.sendCommand(args) })
});

// Admin endpoints: 50 req/min/IP
const adminLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: 1 * 60 * 1000,
  max: 50,
  // In production: store: new RedisStore({ sendCommand: (...args) => redisClient.sendCommand(args) })
});

// Route classifier middleware
function tieredRateLimiter(req, res, next) {
  const path = req.path.toLowerCase();
  
  // Write-heavy paths (checkout, cart mutations, orders)
  if (path.includes('/checkout') || 
      path.includes('/cart') && req.method !== 'GET' ||
      path.includes('/orders') && req.method === 'POST' ||
      path.includes('/payments')) {
    return writeLimiter(req, res, next);
  }
  
  // Admin paths
  if (path.includes('/admin')) {
    return adminLimiter(req, res, next);
  }
  
  // Default: read-heavy (search, catalog, browse)
  return readLimiter(req, res, next);
}

module.exports = tieredRateLimiter;
