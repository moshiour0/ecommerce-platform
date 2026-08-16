'use strict';

// Unit tests for read-model field ownership, Node side.
//
// Run with:  npm test          (from shared/libs/node-common)
//
// Bare `node --test`, with no path argument, for the same reason as
// services/api-gateway: naming the files works on one Node version and not the
// other. node:test ships with Node 18+, so this adds no dependency.

const test = require('node:test');
const assert = require('node:assert/strict');

const rm = require('../read_model');

const { CATALOG, PRICING, INVENTORY, OwnershipError } = rm;

/** Records the call instead of making it. */
function fakeEs() {
  const calls = [];
  return { calls, update(args) { calls.push(args); return { result: 'updated' }; } };
}

// ---------------------------------------------------------------------------
// the three bugs, as tests
// ---------------------------------------------------------------------------

test('catalog cannot write the price', () => {
  assert.throws(() => rm.validateProductWrite(CATALOG, ['name', 'price_cents']),
    /pricing-service/);
});

test('catalog cannot write stock', () => {
  assert.throws(() => rm.validateProductWrite(CATALOG, ['quantity_available']),
    /inventory-service/);
});

test('pricing cannot write catalog fields', () => {
  assert.throws(() => rm.validateProductWrite(PRICING, ['price_cents', 'is_active']),
    OwnershipError);
});

test('inventory cannot write the price', () => {
  assert.throws(() => rm.validateProductWrite(INVENTORY, ['price_cents']),
    OwnershipError);
});

test('the error names the real owner', () => {
  // So whoever hits this knows who to talk to rather than deleting the check.
  assert.throws(() => rm.validateProductWrite(CATALOG, ['price_cents']),
    (err) => err instanceof OwnershipError && /pricing-service/.test(err.message));
});

// ---------------------------------------------------------------------------
// what each owner may write
// ---------------------------------------------------------------------------

test('each owner can write its own fields', () => {
  rm.validateProductWrite(CATALOG, ['sku', 'name', 'description', 'is_active',
    'base_price_cents', 'catalog_updated_at']);
  rm.validateProductWrite(PRICING, ['price_cents']);
  rm.validateProductWrite(INVENTORY, ['quantity_available']);
});

test('the shared timestamp is writable by everyone', () => {
  // Which is exactly why it cannot say whose write was last, and why
  // catalog_updated_at exists.
  for (const owner of [CATALOG, PRICING, INVENTORY]) {
    rm.validateProductWrite(owner, ['updated_at']);
  }
});

test('no field has two owners', () => {
  const sets = [CATALOG, PRICING, INVENTORY].map(
    (o) => new Set(rm.fieldsOwnedBy(o).filter((f) => f !== 'updated_at')));
  for (let i = 0; i < sets.length; i += 1) {
    for (let j = i + 1; j < sets.length; j += 1) {
      const shared = [...sets[i]].filter((f) => sets[j].has(f));
      assert.deepEqual(shared, [], `fields claimed twice: ${shared}`);
    }
  }
});

test('owned and foreign partition the document', () => {
  for (const owner of [CATALOG, PRICING, INVENTORY]) {
    const owned = new Set(rm.fieldsOwnedBy(owner));
    const foreign = rm.foreignFields(owner);
    assert.equal(foreign.some((f) => owned.has(f)), false);
    assert.equal(owned.size + foreign.length,
      Object.keys(rm.PRODUCT_FIELD_OWNERS).length);
  }
});

test('an empty write is allowed', () => {
  rm.validateProductWrite(CATALOG, []);
});

// ---------------------------------------------------------------------------
// typos
// ---------------------------------------------------------------------------

test('unknown fields are refused', () => {
  // Elasticsearch maps new fields dynamically, so a typo creates a second field
  // beside the real one and nothing looks wrong until a search returns nothing.
  assert.throws(() => rm.validateProductWrite(INVENTORY, ['quantity_avaliable']),
    /nobody owns/);
});

test('an unknown writer is refused', () => {
  assert.throws(() => rm.validateProductWrite('some-new-service', ['name']),
    /unknown writer/);
});

// ---------------------------------------------------------------------------
// the write itself
// ---------------------------------------------------------------------------

test('the body is always a partial upsert', () => {
  const body = rm.productUpdateBody(PRICING, { price_cents: 1500 });
  assert.equal(body.doc_as_upsert, true);
  assert.deepEqual(body.doc, { price_cents: 1500 });
  assert.equal('document' in body, false);
});

test('the body copies its input', () => {
  const fields = { price_cents: 1500 };
  const body = rm.productUpdateBody(PRICING, fields);
  body.doc.price_cents = 9999;
  assert.equal(fields.price_cents, 1500);
});

test('writeProduct sends a partial update', () => {
  const es = fakeEs();
  rm.writeProduct(es, 'p-1', PRICING, { price_cents: 1500 });
  assert.equal(es.calls[0].id, 'p-1');
  assert.equal(es.calls[0].index, rm.PRODUCTS_INDEX);
  assert.equal(es.calls[0].body.doc_as_upsert, true);
});

test('writeProduct always records the product id', () => {
  const es = fakeEs();
  rm.writeProduct(es, 'p-1', INVENTORY, { quantity_available: 3 });
  assert.equal(es.calls[0].body.doc.product_id, 'p-1');
});

test('writeProduct refuses a foreign field before calling elasticsearch', () => {
  const es = fakeEs();
  assert.throws(() => rm.writeProduct(es, 'p-1', INVENTORY, { price_cents: 1 }),
    OwnershipError);
  assert.deepEqual(es.calls, [], 'a refused write still reached Elasticsearch');
});

test('there is no whole-document write function', () => {
  // The point of the module. If one existed it would eventually be used.
  for (const forbidden of ['indexProduct', 'replaceProduct', 'putProduct',
    'setProduct']) {
    assert.equal(forbidden in rm, false, `${forbidden} must not exist`);
  }
});
