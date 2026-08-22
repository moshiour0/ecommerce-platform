const express = require('express');
const {
  cartClient,
  catalogClient,
  pricingClient,
  fraudClient,
  deliveryQuoteClient,
  orderSagaClient,
  sellerClient
} = require('../clients/downstream');
const { sellerScope } = require('node-common');
const logger = require('../utils/logger');
const { v4: uuidv4 } = require('uuid');

const router = express.Router();

// POST /api/checkout/:user_id
router.post('/:user_id', async (req, res, next) => {
  try {
    const { user_id } = req.params;
    const idempotencyKey = req.headers['idempotency-key'];

    // Telemetry and other data from client payload
    const {
      telemetry,
      destination_region = "DefaultRegion",
      weight_grams = 1000
    } = req.body;

    if (!idempotencyKey) {
      return res.status(400).json({ detail: 'Idempotency-Key header is required' });
    }

    logger.info(`BFF Checkout started for user: ${user_id}`);

    // 1. Fetch Cart
    logger.info(`Fetching cart for user: ${user_id}`);
    let cartResponse;
    try {
      cartResponse = await cartClient.get(`/cart/${user_id}`);
    } catch (err) {
      logger.error(`Cart fetch failed: ${err.message}`);
      return res.status(err.response?.status || 500).json({ detail: "Failed to fetch cart" });
    }

    const cart = cartResponse.data;
    if (!cart.items || cart.items.length === 0) {
      return res.status(400).json({ detail: "Cart is empty" });
    }

    const cart_id = cart.cart_id; // UUID from cart response

    // 2. Validate against Catalog & Pricing
    logger.info(`Validating cart items against Catalog and Pricing...`);

    // Parallel validation: fetch catalog + pricing for all items concurrently
    const validationPromises = cart.items.map(async (item) => {
      // Validate Catalog
      const catalogRes = await catalogClient.get(`/products/${item.product_id}`);
      if (!catalogRes.data.is_active) {
        throw { status: 400, detail: `Product ${item.product_id} is no longer active` };
      }

      // Validate Pricing
      const pricingRes = await pricingClient.get(`/prices/${item.product_id}`);
      const livePrice = pricingRes.data.base_price_cents;

      // seller_id comes from catalog, which is already being asked about this
      // product for validation. order-saga splits the order by it, and refuses
      // the whole checkout if any line arrives without one -- a line nobody
      // can be paid for is a line nobody can be asked to ship.
      const sellerId = catalogRes.data.seller_id;
      if (!sellerId) {
        throw {
          status: 400,
          detail: `Product ${item.product_id} has no seller and cannot be ordered`
        };
      }

      return {
        product_id: item.product_id,
        seller_id: sellerId,
        quantity: item.quantity,
        price_cents: livePrice,
        line_total_cents: livePrice * item.quantity
      };
    });

    let validatedItems;
    try {
      validatedItems = await Promise.all(validationPromises);
    } catch (err) {
      if (err.status) {
        return res.status(err.status).json({ detail: err.detail });
      }
      logger.error(`Catalog/Pricing validation failed: ${err.message}`);
      return res.status(400).json({ detail: `Validation failed: ${err.message}` });
    }

    // Every seller in this cart must still be allowed to receive orders.
    //
    // Listing enforcement was creation-only: catalog-service asks whether a
    // seller may list at the moment a product is created, and nothing ever
    // asked again. So suspending a seller left their entire existing
    // catalogue live *and buyable*, which is the half that matters -- a
    // suspension that does not stop orders is not a suspension.
    //
    // Asked once per distinct seller rather than once per line: it is a fact
    // about the seller, and a cart of ten items from one shop is one question.
    //
    // Fails closed. An unreachable seller-service refuses the checkout rather
    // than letting it through, because otherwise an outage becomes the way to
    // buy from a banned seller, and outages are cheap to cause. That is the
    // same direction catalog-service fails on the listing check.
    const distinctSellers = [...new Set(validatedItems.map(i => i.seller_id))];
    const blocked = [];
    await Promise.all(distinctSellers.map(async (sid) => {
      let permission = null;
      try {
        const res = await sellerClient.get(`/sellers/${sid}/permission`);
        permission = res.data;
      } catch (err) {
        logger.warn(`Seller ${sid} permission unavailable: ${err.message}; `
                  + `refusing rather than selling unchecked`);
      }
      if (!sellerScope.maySellTo(permission)) {
        blocked.push(sid);
      }
    }));

    if (blocked.length > 0) {
      const productIds = validatedItems
        .filter(i => blocked.includes(i.seller_id))
        .map(i => i.product_id);
      logger.warn(`Checkout refused: ${blocked.length} seller(s) may not `
                + `receive orders`);
      return res.status(409).json({
        detail: sellerScope.sellerRefusalDetail(productIds),
        unavailable_product_ids: productIds
      });
    }

    const cartSumCents = validatedItems.reduce((sum, item) => sum + item.line_total_cents, 0);

    // 3. Evaluate Fraud
    logger.info(`Evaluating Fraud Telemetry for user: ${user_id}`);
    try {
      // Create a unique fraud check key based on idempotency key to prevent double checks
      const fraudIdempotency = `fraud-${idempotencyKey}`;
      const fraudRes = await fraudClient.post(`/fraud/evaluate`, {
        user_id: user_id,
        telemetry: telemetry || {}
      }, {
        headers: { 'Idempotency-Key': fraudIdempotency }
      });

      if (fraudRes.data.decision === "REJECTED") {
        logger.warn(`Checkout rejected by Fraud Service for user: ${user_id}`);
        return res.status(403).json({ detail: `Transaction declined. Reason: ${fraudRes.data.reason}` });
      }
    } catch (err) {
      logger.error(`Fraud check failed: ${err.message}`);
      return res.status(500).json({ detail: "Fraud evaluation failed" });
    }

    // 4. Fetch Delivery Quote
    logger.info(`Fetching delivery quote for region: ${destination_region}`);
    let deliveryAmountCents = 0;
    try {
      const deliveryIdempotency = `deliv-${idempotencyKey}`;
      const deliveryRes = await deliveryQuoteClient.post(`/quotes`, {
        cart_id: cart_id,
        user_id: user_id,
        destination_region: destination_region,
        weight_grams: weight_grams
      }, {
        headers: { 'Idempotency-Key': deliveryIdempotency }
      });
      deliveryAmountCents = deliveryRes.data.amount_cents;
    } catch (err) {
      logger.error(`Delivery quote fetch failed: ${err.message}`);
      return res.status(500).json({ detail: "Failed to generate delivery quote" });
    }

    const finalTotalCents = cartSumCents + deliveryAmountCents;
    logger.info(`Cart Sum: ${cartSumCents}, Delivery: ${deliveryAmountCents}, Total: ${finalTotalCents}`);

    // 5. Submit to Order Saga
    logger.info(`Submitting order to Saga for user: ${user_id}`);
    try {
      const orderRes = await orderSagaClient.post(`/orders`, {
        user_id: user_id,
        total_cents: finalTotalCents,
        items_payload: validatedItems
      }, {
        headers: { 'Idempotency-Key': idempotencyKey }
      });

      logger.info(`Order Saga accepted checkout. Saga ID: ${orderRes.data.id}`);
      return res.status(201).json(orderRes.data);
    } catch (err) {
      logger.error(`Order Saga submission failed: ${err.message}`);
      if (err.response && err.response.status === 409) {
        // Idempotency conflict from saga
        return res.status(409).json({ detail: "Order already processed" });
      }
      return res.status(500).json({ detail: "Failed to submit order" });
    }

  } catch (error) {
    logger.error(`Unexpected error in Checkout BFF: ${error.message}`);
    next(error);
  }
});

module.exports = router;
