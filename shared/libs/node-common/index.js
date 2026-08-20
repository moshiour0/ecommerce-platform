'use strict';

// Shared Node platform library. Mirrors shared/libs/python-common.
// Copied into each Node image by docker/Dockerfile.node.
module.exports = {
  ...require('./resilience'),
  // Where Redis is, decided from the environment. Pure -- no client library,
  // so requiring node-common never drags ioredis in.
  redisRules: require('./redis_rules'),
  // The client itself is behind a getter on purpose. It requires ioredis, and
  // this index is loaded by tests and tools that have no Redis dependency at
  // all; an eager require would make node-common unloadable for them.
  get redisClient() {
    return require('./redis_client');
  },
  // Field ownership for the products read model. No Node service writes
  // that index yet; this is here so the first one cannot repeat the
  // whole-document write that cost the Python side three separate bugs.
  readModel: require('./read_model')
};
