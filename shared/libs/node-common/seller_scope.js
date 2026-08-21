'use strict';

// Who a seller request is for, and what a seller is allowed to do.
//
// Pure functions, because both questions are exactly the kind that get
// answered inline in a route handler and then answered differently in the next
// route handler.
//
// The identity rule
// -----------------
// A seller's id comes from the verified token and from nowhere else. Never
// from the path, never from the query string, never from the body. A BFF that
// accepts `GET /api/seller/orders?seller_id=...` is a BFF where every seller
// can read every other seller's orders, balance and customer addresses by
// changing one number -- and it looks completely normal in a log.
//
// So `resolveSellerId` reads one header and refuses everything else, and
// `rejectsCallerSuppliedSeller` exists so a route can say *why* rather than
// silently ignoring the parameter. Silently ignoring it is nearly as bad: the
// caller believes they scoped the request, and the response looks like an
// answer to the question they asked.
//
// The action rule
// ---------------
// A seller may move their own order forward through the parts of the lifecycle
// they actually perform. They may not attest to things only somebody else can
// witness -- above all `deliver`, which consumes stock and books escrow. A
// seller who can mark their own parcel delivered can trigger their own payout
// for goods still sitting in their shop.

// The header the gateway sets from the verified token's seller claim.
const SELLER_HEADER = 'x-seller-id';

// Ways a caller might try to name a seller themselves. Present so a route can
// refuse them explicitly rather than ignoring them.
const CALLER_SUPPLIED_KEYS = ['seller_id', 'sellerId', 'seller'];

// What a seller may do to their own seller order.
//
// `confirm`  -- accepting the order is the seller's decision.
// `dispatch` -- handing the parcel to a courier is a thing the seller does.
// `cancel`   -- refusing before dispatch is legitimate: they are out of stock,
//               and the alternative is a courier collecting nothing.
const SELLER_ACTIONS = Object.freeze(['confirm', 'dispatch', 'cancel']);

// What a seller may not do, and why. Kept as data rather than as an `if`,
// because the reason is the useful part of the refusal.
const FORBIDDEN_ACTIONS = Object.freeze({
  deliver:
    'a delivery is attested by the courier, not claimed by the seller: it ' +
    'consumes stock and books escrow, so a seller who could mark their own ' +
    'parcel delivered could trigger their own payout',
  settle:
    'settlement is the platform reconciling cash a courier remitted; a ' +
    'seller declaring themselves settled would be declaring they had been paid',
  mark_rto:
    'a refusal at the door is something the courier witnessed',
  complete_return:
    'a return is confirmed when the goods are physically back, which the ' +
    'seller is not the only party to'
});

/**
 * The authenticated seller, or null.
 *
 * Reads exactly one header. A caller cannot influence this by any other route.
 */
function resolveSellerId(headers) {
  const raw = (headers || {})[SELLER_HEADER];
  if (typeof raw !== 'string') return null;
  const value = raw.trim();
  return value === '' ? null : value;
}

/**
 * Whether the caller tried to name a seller themselves.
 *
 * Returns the offending key, or null. Used to refuse explicitly: a request
 * that carries `?seller_id=` is either a client bug or an attempt, and both
 * deserve an answer rather than a response scoped to somebody else.
 */
function callerSuppliedSeller(sources) {
  for (const source of sources || []) {
    if (!source || typeof source !== 'object') continue;
    for (const key of CALLER_SUPPLIED_KEYS) {
      if (Object.prototype.hasOwnProperty.call(source, key)) return key;
    }
  }
  return null;
}

/** Whether a seller may perform this action on their own order. */
function isSellerAction(action) {
  return SELLER_ACTIONS.includes(action);
}

/**
 * Why an action is refused, or null if it is allowed.
 *
 * A named reason for the forbidden ones and a generic answer for anything
 * unrecognised -- an action nobody has heard of must not fall through to
 * allowed.
 */
function refusalReason(action) {
  if (isSellerAction(action)) return null;
  if (Object.prototype.hasOwnProperty.call(FORBIDDEN_ACTIONS, action)) {
    return FORBIDDEN_ACTIONS[action];
  }
  return `unknown action "${action}"`;
}

/**
 * Whether a seller order belongs to this seller.
 *
 * Compared as strings and trimmed, because one side comes from a JWT claim and
 * the other from a JSON response, and a stray space would silently answer
 * "not yours" for a seller's own order.
 */
function ownsSellerOrder(sellerId, sellerOrder) {
  if (!sellerId || !sellerOrder) return false;
  const owner = sellerOrder.seller_id;
  if (typeof owner !== 'string') return false;
  return owner.trim() === String(sellerId).trim();
}

module.exports = {
  SELLER_HEADER,
  SELLER_ACTIONS,
  FORBIDDEN_ACTIONS,
  CALLER_SUPPLIED_KEYS,
  resolveSellerId,
  callerSuppliedSeller,
  isSellerAction,
  refusalReason,
  ownsSellerOrder
};
