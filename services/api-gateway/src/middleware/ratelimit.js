const rateLimit = require('express-rate-limit');
const { RedisStore } = require('rate-limit-redis');
const { createClient } = require('redis');
const logger = require('../utils/logger');
const {
  WINDOW_MS,
  BUDGETS,
  isExempt,
  classify,
  requestPath,
} = require('./ratelimit_rules');

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
// tiers increment the same counter and the effective limit collapses to the
// smallest one (20/min) across every endpoint.
function tierStore(prefix) {
  return new RedisStore({
    sendCommand: (...args) => redisClient.sendCommand(args),
    prefix
  });
}

// Read endpoints: 200 req/min/IP
const readLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: WINDOW_MS,
  max: BUDGETS.read,
  store: tierStore('rl:read:')
});

// Write endpoints: 20 req/min/IP
const writeLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: WINDOW_MS,
  max: BUDGETS.write,
  store: tierStore('rl:write:')
});

// Admin, pre-auth: 50 req/min/IP.
//
// The per-user admin limiter below can only run after verifyToken, and an
// unauthenticated flood never reaches it — those requests are rejected with 401
// first. Without this IP-keyed cap in front, moving admin limiting after auth
// would leave anonymous floods against /admin entirely unlimited. Both counters
// apply to an authenticated admin request; the tighter one binds, which is the
// intent.
const adminIpLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: WINDOW_MS,
  max: BUDGETS.admin,
  store: tierStore('rl:adminip:')
});

// Admin, post-auth: 50 req/min/user, as the architecture specifies.
//
// This is mounted after verifyToken in index.js, so req.user is populated and
// the key is the user id. It previously ran before auth, where req.user was
// always undefined and the tier silently degraded to per-IP — meaning several
// admins behind one office NAT shared a single budget while one admin on a
// mobile network got a fresh budget per IP change.
const adminUserLimiter = rateLimit({
  ...rateLimitConfig,
  windowMs: WINDOW_MS,
  max: BUDGETS.admin,
  keyGenerator: (req) => (req.user && (req.user.sub || req.user.user_id)) || req.ip,
  store: tierStore('rl:admin:')
});

// Perimeter limiter: runs before authentication, so unauthenticated floods are
// capped before they reach JWT verification.
function tieredRateLimiter(req, res, next) {
  const path = requestPath(req);

  // Probe paths bypass every tier. Route ordering in index.js already keeps
  // /health in front of this middleware; the check is repeated here so that
  // re-ordering the routes cannot quietly put liveness back behind a counter.
  if (isExempt(path)) return next();

  switch (classify(path, req.method)) {
    case 'write': return writeLimiter(req, res, next);
    case 'admin': return adminIpLimiter(req, res, next);
    default: return readLimiter(req, res, next);
  }
}

// Per-user admin limiter, mounted after verifyToken. Non-admin paths pass
// through untouched — they were already counted by the perimeter limiter.
function adminRateLimiter(req, res, next) {
  const path = requestPath(req);
  if (isExempt(path)) return next();
  if (classify(path, req.method) !== 'admin') return next();
  return adminUserLimiter(req, res, next);
}

module.exports = { tieredRateLimiter, adminRateLimiter, redisReady, redisClient };
