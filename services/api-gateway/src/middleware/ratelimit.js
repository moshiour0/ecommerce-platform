const rateLimit = require('express-rate-limit');
const { RedisStore } = require('rate-limit-redis');
const { createClient } = require('redis');
const logger = require('../utils/logger');

// Rule 8 (Security by Default): no hardcoded fallbacks for infrastructure the
// security posture depends on. A rate limiter backed by per-replica memory is
// not a rate limiter at scale — it is an N-times-larger budget for an attacker.
// Consistent with auth.js: refuse to start rather than run degraded.
const REDIS_URL = process.env.REDIS_URL;
if (!REDIS_URL) {
  throw new Error(
    'FATAL: REDIS_URL environment variable is not set. ' +
    'The rate limiter requires a shared store; refusing to start with per-replica memory.'
  );
}

const redisClient = createClient({ url: REDIS_URL });

redisClient.on('error', (err) => {
  // Do NOT fall back to MemoryStore here. Silent degradation to per-replica
  // counters is exactly the bypass this file exists to prevent.
  logger.error(`Rate limiter Redis error: ${err.message}`);
});

// Connect eagerly. node-redis queues commands issued while a connect() is
// in flight, so middleware registered below is safe to reference the client.
const redisReady = redisClient
  .connect()
  .then(() => logger.info(`Rate limiter connected to shared Redis store at ${REDIS_URL}`))
  .catch((err) => {
    logger.error(`FATAL: rate limiter could not reach Redis: ${err.message}`);
    throw err;
  });

const rateLimitConfig = {
  standardHeaders: true,
  legacyHeaders: false,
  handler: (req, res, next, options) => {
    logger.warn(`Rate limit exceeded for IP: ${req.ip} on path: ${req.path}`);
    res.status(options.statusCode).send(options.message);
  },
  message: { detail: 'Too many requests, please try again later.' }
};

// Each tier needs its own key prefix. With a shared store and no prefix, all
// three tiers increment the same counter and the effective limit collapses to
// the smallest one (20/min) across every endpoint.
function tierStore(prefix) {
  return new RedisStore({
    sendCommand: (...args) => redisClient.sendCommand(args),
    prefix
  });
}

// Tiered Rate Limiting (Architecture Rule: api-gateway domain boundary)
// Read endpoints: 200 req/min/IP
const readLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: 1 * 60 * 1000,
  max: 200,
  store: tierStore('rl:read:')
});

// Write endpoints: 20 req/min/IP
const writeLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: 1 * 60 * 1000,
  max: 20,
  store: tierStore('rl:write:')
});

// Admin endpoints: 50 req/min/user (per architecture, not per IP).
// NOTE: this middleware currently runs before verifyToken in index.js, so
// req.user is undefined and this degrades to per-IP. See ORDERING note there.
const adminLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: 1 * 60 * 1000,
  max: 50,
  keyGenerator: (req) => (req.user && (req.user.sub || req.user.user_id)) || req.ip,
  store: tierStore('rl:admin:')
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

module.exports = { tieredRateLimiter, redisReady, redisClient };
