'use strict';

// Who a seller request is for, and what a seller may do.
//
// Both of these are the kind of rule that gets written once in a route handler
// and then written slightly differently in the next one. The first is an
// authorisation boundary and the second is a payout control, so they are here
// as data with tests rather than as two `if`s in a router.

const test = require('node:test');
const assert = require('node:assert');

const {
  SELLER_HEADER,
  SELLER_ACTIONS,
  FORBIDDEN_ACTIONS,
  resolveSellerId,
  callerSuppliedSeller,
  isSellerAction,
  refusalReason,
  ownsSellerOrder,
  maySellTo,
  sellerRefusalDetail
} = require('../seller_scope');

// ---------------------------------------------------------------------------
// identity comes from the token and nowhere else
// ---------------------------------------------------------------------------

test('the seller comes from the gateway header', () => {
  assert.strictEqual(resolveSellerId({ [SELLER_HEADER]: 'seller-1' }), 'seller-1');
});

test('a missing header is nobody, not a default', () => {
  assert.strictEqual(resolveSellerId({}), null);
  assert.strictEqual(resolveSellerId(undefined), null);
  assert.strictEqual(resolveSellerId({ [SELLER_HEADER]: '' }), null);
  assert.strictEqual(resolveSellerId({ [SELLER_HEADER]: '   ' }), null);
});

test('a non-string header is nobody', () => {
  // A header injected as an array by a proxy must not become an identity.
  assert.strictEqual(resolveSellerId({ [SELLER_HEADER]: ['a', 'b'] }), null);
  assert.strictEqual(resolveSellerId({ [SELLER_HEADER]: 42 }), null);
});

test('a seller named in the query string is caught, not ignored', () => {
  // THE test in this file. `?seller_id=` reaching a handler that silently
  // ignores it leaves the caller believing they scoped the request, and the
  // response looks like an answer to the question they asked.
  assert.strictEqual(callerSuppliedSeller([{ seller_id: 'other' }]), 'seller_id');
  assert.strictEqual(callerSuppliedSeller([{ sellerId: 'other' }]), 'sellerId');
  assert.strictEqual(callerSuppliedSeller([{}, { seller: 'other' }]), 'seller');
});

test('a request that names nobody is clean', () => {
  assert.strictEqual(callerSuppliedSeller([{ status: 'CONFIRMED' }]), null);
  assert.strictEqual(callerSuppliedSeller([]), null);
  assert.strictEqual(callerSuppliedSeller([null, undefined]), null);
});

test('ownership is compared as trimmed strings', () => {
  // One side is a JWT claim and the other a JSON field; a stray space would
  // answer "not yours" for a seller's own order.
  assert.ok(ownsSellerOrder('seller-1', { seller_id: 'seller-1' }));
  assert.ok(ownsSellerOrder(' seller-1 ', { seller_id: 'seller-1' }));
  assert.ok(!ownsSellerOrder('seller-1', { seller_id: 'seller-2' }));
});

test('an order with no owner belongs to nobody', () => {
  assert.ok(!ownsSellerOrder('seller-1', {}));
  assert.ok(!ownsSellerOrder('seller-1', { seller_id: null }));
  assert.ok(!ownsSellerOrder(null, { seller_id: 'seller-1' }));
});

// ---------------------------------------------------------------------------
// what a seller may do
// ---------------------------------------------------------------------------

test('a seller may move their own order forward', () => {
  for (const action of ['confirm', 'dispatch', 'cancel']) {
    assert.ok(isSellerAction(action), `${action} should be allowed`);
    assert.strictEqual(refusalReason(action), null);
  }
});

test('a seller may not mark their own parcel delivered', () => {
  // The payout control. deliver consumes stock and books escrow, so a seller
  // who could claim it could trigger their own payout for goods still on
  // their shelf.
  assert.ok(!isSellerAction('deliver'));
  assert.match(refusalReason('deliver'), /own payout/);
});

test('a seller may not settle, RTO or complete a return', () => {
  for (const action of ['settle', 'mark_rto', 'complete_return']) {
    assert.ok(!isSellerAction(action), `${action} must be refused`);
    assert.ok(refusalReason(action), `${action} needs a stated reason`);
  }
});

test('every forbidden action explains itself', () => {
  // "Forbidden" alone is a support ticket. The reason is the useful part.
  for (const [action, reason] of Object.entries(FORBIDDEN_ACTIONS)) {
    assert.ok(reason.length > 20, `${action} has no real explanation`);
    assert.strictEqual(refusalReason(action), reason);
  }
});

test('an unknown action is refused rather than falling through', () => {
  assert.ok(!isSellerAction('teleport'));
  assert.match(refusalReason('teleport'), /unknown action/);
  assert.ok(!isSellerAction(''));
  assert.ok(!isSellerAction(undefined));
});

test('the allowlist and the forbidden list do not overlap', () => {
  for (const action of SELLER_ACTIONS) {
    assert.ok(!(action in FORBIDDEN_ACTIONS),
      `${action} is both allowed and forbidden`);
  }
});

test('the allowlist is frozen', () => {
  // A route handler pushing onto it at runtime would widen a payout control
  // for the life of the process.
  assert.throws(() => SELLER_ACTIONS.push('deliver'));
});

// ---------------------------------------------------------------------------
// may this seller still be sold from?
// ---------------------------------------------------------------------------
//
// Listing enforcement was creation-only: catalog-service asked whether a
// seller could list at the moment a product was created and nothing ever asked
// again, so suspending a seller left their whole catalogue live and buyable.
// A suspension that does not stop orders is not a suspension.

test('an active seller may be sold from', () => {
  assert.equal(maySellTo({ may_receive_orders: true }), true);
});

test('a suspended seller may not', () => {
  assert.equal(maySellTo({ may_receive_orders: false }), false);
});

test('permission must be exactly true, not merely truthy', () => {
  // A payload that changed shape and now returns a string would otherwise
  // read as consent.
  for (const value of ['yes', 'true', 1, {}, [], 'false']) {
    assert.equal(maySellTo({ may_receive_orders: value }), false,
      `${JSON.stringify(value)} was treated as permission`);
  }
});

test('it fails closed on anything unusable', () => {
  // An outage must not become the way to buy from a banned seller.
  for (const value of [null, undefined, {}, 'ok', 42, []]) {
    assert.equal(maySellTo(value), false);
  }
});

test('the refusal does not leak which seller is suspended', () => {
  // A buyer cannot fix a seller's suspension, and naming the status would
  // leak one merchant's standing to anyone willing to add their product to a
  // cart.
  const detail = sellerRefusalDetail(['p-1']);
  assert.ok(!/suspend|ban|status/i.test(detail), detail);
});

test('the refusal reads correctly for one item and for several', () => {
  assert.match(sellerRefusalDetail(['p-1']), /An item is/);
  assert.match(sellerRefusalDetail(['p-1', 'p-2']), /Some items are/);
});
