require('dotenv').config();
const express = require('express');
const cors = require('cors');
const { createProxyMiddleware } = require('http-proxy-middleware');
const logger = require('./utils/logger');
const { tieredRateLimiter, adminRateLimiter, redisReady } = require('./middleware/ratelimit');
const { parseTrustProxy, isSpoofableTrustProxy } = require('./middleware/ratelimit_rules');
const verifyToken = require('./middleware/auth');

const app = express();
const PORT = process.env.PORT || 8000;

const BFF_SHOP_URL = process.env.BFF_SHOP_URL || 'http://bff-shop:8001';
const BFF_CHECKOUT_URL = process.env.BFF_CHECKOUT_URL || 'http://bff-checkout:8002';
const BFF_SELLER_URL = process.env.BFF_SELLER_URL || 'http://bff-seller:8021';

// 0. Proxy trust — decides what req.ip is, and therefore what the rate limiter
// counts. Defaults to false, which is correct while the gateway is reached
// directly: every service is ClusterIP and there is no Ingress. Put an Ingress
// or load balancer in front and this MUST become the number of proxy hops, or
// every client collapses into the load balancer's single 200/min bucket.
const TRUST_PROXY = parseTrustProxy(process.env.TRUST_PROXY);
app.set('trust proxy', TRUST_PROXY);
if (isSpoofableTrustProxy(TRUST_PROXY)) {
  logger.warn(
    'TRUST_PROXY=true trusts a client-supplied X-Forwarded-For, so a caller can ' +
    'present a new IP per request and bypass rate limiting entirely. Prefer the ' +
    'number of proxy hops (e.g. TRUST_PROXY=1).'
  );
}

// 1. Basic Middleware
app.use(cors());

// 2. Health Check (Public, and deliberately ahead of the limiter)
// ORDERING: the Kubernetes readiness AND liveness probes both call this path.
// Behind the limiter, a flood from one IP exhausted the read budget and /health
// started returning 429 — which kubelet reads as a failed liveness probe and
// answers by restarting the gateway. The limiter has to absorb a flood, not
// convert it into a restart loop. ratelimit.js repeats the exemption so that
// re-ordering these lines cannot silently undo it.
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

// 3. Global Rate Limiting (Redis-backed, shared across replicas)
// ORDERING: this runs before verifyToken so unauthenticated floods are capped
// before they reach JWT verification, and before request logging so a flood
// cannot amplify itself into log volume.
app.use(tieredRateLimiter);

// 4. Request Logging
app.use((req, res, next) => {
  logger.info(`Incoming Request: ${req.method} ${req.originalUrl} from ${req.ip}`);
  next();
});

// 5. Authentication Verification (Applied before proxying)
app.use('/api', verifyToken);

// 6. Per-user admin limiting, which is only possible now that verifyToken has
// populated req.user. Admin requests are counted twice on purpose: per IP at
// the perimeter above (which is all an anonymous flood ever reaches) and per
// user here.
app.use('/api', adminRateLimiter);

// 7. Proxy Configuration

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

// Proxy /api/seller to bff-seller
//
// The seller's identity is set HERE, from the verified token, and is the only
// way bff-seller learns who is calling. A caller cannot supply it: any
// x-seller-id on the inbound request is overwritten, and bff-seller refuses a
// request that names a seller in the query or body.
//
// Without the claim there is no header, and bff-seller answers 401. A buyer's
// token is not a seller's token, and treating one as the other would give
// every logged-in customer a seller dashboard.
app.use('/api/seller', createProxyMiddleware({
  target: BFF_SELLER_URL,
  changeOrigin: true,
  pathRewrite: {
    '^/api/seller': '/api/seller'
  },
  onProxyReq: (proxyReq, req, res) => {
    // Strip anything the caller sent, unconditionally, before deciding what
    // to set. Overwriting only when a claim exists would let a forged header
    // survive on a token that has none.
    proxyReq.removeHeader('x-seller-id');
    if (req.user && req.user.seller_id) {
      proxyReq.setHeader('x-seller-id', String(req.user.seller_id));
    }
    if (req.user) {
      proxyReq.setHeader('x-user-id', req.user.sub || req.user.user_id || 'unknown');
    }
  },
  onError: (err, req, res) => {
    logger.error(`Proxy error to bff-seller: ${err.message}`);
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

// Do not accept traffic until the shared rate-limit store is reachable.
// Serving requests with an unreachable limiter store would either 500 every
// request or, worse, invite a silent memory fallback.
redisReady
  .then(() => {
    app.listen(PORT, () => {
      logger.info(`API Gateway listening on port ${PORT}`);
    });
  })
  .catch((err) => {
    logger.error(`API Gateway failed to start: ${err.message}`);
    process.exit(1);
  });
