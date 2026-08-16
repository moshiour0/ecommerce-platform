'use strict';

// Shared Node platform library. Mirrors shared/libs/python-common.
// Copied into each Node image by docker/Dockerfile.node.
module.exports = {
  ...require('./resilience'),
  // Field ownership for the products read model. No Node service writes
  // that index yet; this is here so the first one cannot repeat the
  // whole-document write that cost the Python side three separate bugs.
  readModel: require('./read_model')
};
