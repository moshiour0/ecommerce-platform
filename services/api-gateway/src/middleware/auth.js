const jwt = require('jsonwebtoken');
const logger = require('../utils/logger');

const JWT_SECRET = process.env.JWT_SECRET;
if (!JWT_SECRET) {
  throw new Error('FATAL: JWT_SECRET environment variable is not set. Refusing to start with insecure defaults.');
}

// Define paths that do not require authentication
const PUBLIC_PATHS = [
  '/api/shop/search',
  '/api/shop/products',
  '/health'
];

function isPublicPath(path) {
  return PUBLIC_PATHS.some(publicPath => path.startsWith(publicPath));
}

function verifyToken(req, res, next) {
  if (isPublicPath(req.path)) {
    return next();
  }

  const authHeader = req.headers['authorization'];
  if (!authHeader) {
    logger.warn(`Unauthorized access attempt to ${req.path}: No Authorization header`);
    return res.status(401).json({ detail: 'Authorization header is required' });
  }

  const parts = authHeader.split(' ');
  if (parts.length !== 2 || parts[0] !== 'Bearer') {
    logger.warn(`Invalid authorization format on ${req.path}`);
    return res.status(401).json({ detail: 'Authorization header must be Bearer token' });
  }

  const token = parts[1];

  try {
    const decoded = jwt.verify(token, JWT_SECRET);
    req.user = decoded;
    
    // Optionally, forward user details to downstream services via headers
    req.headers['x-user-id'] = decoded.sub || decoded.user_id;
    
    next();
  } catch (error) {
    logger.warn(`JWT Verification failed: ${error.message}`);
    return res.status(401).json({ detail: 'Invalid or expired token' });
  }
}

module.exports = verifyToken;
