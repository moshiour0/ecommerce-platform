'use strict';

// The seller dashboard's only entry point.
//
// Rule 7: a BFF is an orchestrator. It fetches, formats and forwards, and it
// holds no business logic of its own. Every decision here is either the
// authorisation boundary (whose data is this) or a lookup in the shared
// allowlist -- both of which live in node-common/seller_scope so they cannot
// drift between two handlers.
//
// The whole reason this service exists is that sellers must not reach internal
// services directly. `order-saga` has an endpoint that marks a seller order
// delivered; it is correct for the courier integration to call it and
// catastrophic for a seller to. This file is where that line is drawn.

const express = require('express');
const { sellerScope } = require('node-common');
const {
  sellerClient, orderClient, catalogClient, paymentClient, fulfillmentClient
} = require('../clients/downstream');
const logger = require('../utils/logger');

const router = express.Router();

const {
  SELLER_HEADER, SELLER_ACTIONS,
  resolveSellerId, callerSuppliedSeller, isSellerAction, refusalReason,
  ownsSellerOrder
} = sellerScope;

/**
 * Establish who this is, and refuse anyone trying to be somebody else.
 *
 * Two separate refusals on purpose. No identity is 401 -- the caller has not
 * proved who they are. Naming a seller in the request is 400 and says so,
 * rather than being quietly ignored: a caller who believes they scoped a
 * request and receives an unscoped answer has been misled by the API.
 */
function requireSeller(req, res, next) {
  const named = callerSuppliedSeller([req.query, req.body]);
  if (named) {
    logger.warn(`Request named a seller via "${named}" on ${req.path}`);
    return res.status(400).json({
      detail: `a seller may not be named by the caller (found "${named}"). ` +
              `The seller is taken from the authenticated session and from ` +
              `nothing else.`
    });
  }

  const sellerId = resolveSellerId(req.headers);
  if (!sellerId) {
    return res.status(401).json({
      detail: `no seller identity on this request; the gateway supplies ` +
              `${SELLER_HEADER} from the verified token`
    });
  }

  req.sellerId = sellerId;
  next();
}

router.use(requireSeller);

/** Turn a downstream failure into something a dashboard can act on. */
function forwardError(res, error, what) {
  const status = error.response && error.response.status;
  if (status) {
    return res.status(status).json(
      error.response.data || { detail: `${what} failed` });
  }
  logger.error(`${what}: ${error.message}`);
  // A circuit that is open or a downstream that is unreachable is temporary,
  // and a dashboard should retry rather than show the seller an error page.
  return res.status(503).json({ detail: `${what} is unavailable; retry shortly` });
}

// ---------------------------------------------------------------------------
// who am I
// ---------------------------------------------------------------------------

router.get('/me', async (req, res) => {
  try {
    const { data } = await sellerClient.get(`/sellers/${req.sellerId}`);
    // Forwarded as-is. The onboarding fields a dashboard needs -- status,
    // missing_documents, needs_contract_acceptance -- are already derived by
    // seller-service, and re-deriving them here is how two services end up
    // telling a seller different things about their own account.
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading your account');
  }
});

// ---------------------------------------------------------------------------
// the order queue
// ---------------------------------------------------------------------------

router.get('/orders', async (req, res) => {
  try {
    const { data } = await orderClient.get(
      `/orders/seller-orders?seller_id=${encodeURIComponent(req.sellerId)}`);
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading your orders');
  }
});

router.get('/orders/:sellerOrderId', async (req, res) => {
  try {
    const { data } = await orderClient.get(
      `/orders/seller-orders/${req.params.sellerOrderId}`);
    // The ownership check, and the reason this endpoint is not a proxy.
    // order-saga answers about any seller order; this service answers only
    // about yours, and a 404 rather than a 403 so a seller cannot enumerate
    // which ids exist.
    if (!ownsSellerOrder(req.sellerId, data)) {
      logger.warn(`Seller ${req.sellerId} asked for seller order ` +
                  `${req.params.sellerOrderId}, which is not theirs`);
      return res.status(404).json({ detail: 'no such order' });
    }
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading that order');
  }
});

// ---------------------------------------------------------------------------
// moving an order forward
// ---------------------------------------------------------------------------

router.post('/orders/:sellerOrderId/:action', async (req, res) => {
  const { sellerOrderId, action } = req.params;

  // The payout control. `deliver` consumes stock and books escrow, so a seller
  // who could claim it could trigger their own payout for goods still on their
  // shelf. The reason is returned rather than a bare 403: a dashboard showing
  // "forbidden" produces a support ticket, and this produces an explanation.
  if (!isSellerAction(action)) {
    logger.warn(`Seller ${req.sellerId} attempted "${action}" on ${sellerOrderId}`);
    return res.status(403).json({
      detail: refusalReason(action),
      allowed: SELLER_ACTIONS
    });
  }

  let sellerOrder;
  try {
    const { data } = await orderClient.get(`/orders/seller-orders/${sellerOrderId}`);
    sellerOrder = data;
  } catch (error) {
    return forwardError(res, error, 'loading that order');
  }

  // Ownership is checked before the action, not after. Acting first and
  // checking later would mean one seller could confirm another's order and
  // then be told they were not allowed to.
  if (!ownsSellerOrder(req.sellerId, sellerOrder)) {
    logger.warn(`Seller ${req.sellerId} attempted "${action}" on ` +
                `${sellerOrderId}, which is not theirs`);
    return res.status(404).json({ detail: 'no such order' });
  }

  try {
    const { data } = await orderClient.post(
      `/orders/${sellerOrder.order_id}/seller-orders/${sellerOrderId}/${action}`,
      {
        reason: req.body && req.body.reason,
        courier_name: req.body && req.body.courier_name,
        tracking_code: req.body && req.body.tracking_code
      });
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, `the ${action}`);
  }
});

// ---------------------------------------------------------------------------
// performance
// ---------------------------------------------------------------------------

// A seller's own fulfilment record -- the same numbers that feed ranking.
// Shown to them because a metric that decides visibility and is invisible to
// the person it judges is a metric nobody can act on.
router.get('/metrics', async (req, res) => {
  try {
    const { data } = await orderClient.get(
      `/orders/seller-metrics?seller_id=${encodeURIComponent(req.sellerId)}`);
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading your performance');
  }
});

// ---------------------------------------------------------------------------
// money
// ---------------------------------------------------------------------------

router.get('/balance', async (req, res) => {
  try {
    const { data } = await paymentClient.get(
      `/escrow/sellers/${req.sellerId}/balance`);
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading your balance');
  }
});

// ---------------------------------------------------------------------------
// catalogue
// ---------------------------------------------------------------------------

router.get('/products', async (req, res) => {
  try {
    const { data } = await catalogClient.get(
      `/products/?seller_id=${encodeURIComponent(req.sellerId)}`);
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading your products');
  }
});

// ---------------------------------------------------------------------------
// couriers
// ---------------------------------------------------------------------------

router.get('/couriers', async (req, res) => {
  try {
    const { data } = await fulfillmentClient.get('/couriers');
    return res.json(data);
  } catch (error) {
    return forwardError(res, error, 'loading couriers');
  }
});

module.exports = router;
