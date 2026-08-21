'use strict';

// Field ownership for the shared product read model, and the only supported way
// to write it. The Node counterpart of python_common/read_model.py.
//
// Why this exists
// ---------------
// The `products` document in Elasticsearch is assembled from several services,
// and this repository has written the same bug three times: the e2e healer PUT
// whole documents and stripped sku, updated_at and every deactivation; the
// reindex worker came one line from writing whole documents built from catalog
// rows; and stream-processor's ProductCreated handler actually did it, so a
// redelivered creation event erased price and stock. Kafka is at-least-once, so
// that redelivery is routine.
//
// Each was written by someone who knew the document had several owners. Knowing
// was not enough, because the dangerous call is shorter to type than the safe
// one and looks like what you want. So this module removes the choice: one write
// function, always a partial update, and it refuses fields the calling service
// does not own.
//
// No Node service writes this index today. It exists so that the first one to
// do so cannot make the mistake the Python side made three times, rather than
// discovering the rule afterwards from a wiped price column.
//
// Kept deliberately in step with the Python table. They are separate files
// because the two libraries are copied into different images and share no path
// at runtime, so tests/unit/test_read_model_parity.py parses this file and
// fails if the two ever disagree.

const PRODUCTS_INDEX = 'products';

const CATALOG = 'catalog-service';
const PRICING = 'pricing-service';
const INVENTORY = 'inventory-service';

// The seller's own signals, denormalised onto every product they sell.
//
// A fourth writer rather than folding these into CATALOG, because they do not
// come from catalog-service and they do not change when a product changes.
// They are a *seller* fact copied onto product documents so Elasticsearch can
// score on them in one query -- the ranking used to fetch them per seller, per
// results page, over HTTP.
//
// Their own owner is what stops a catalog update from silently blanking a
// rating: a ProductCreated redelivery writes CATALOG's fields and leaves
// anything it does not name alone.
const SELLER_SIGNALS = 'seller-projection';

// Written by every owner, and so owned by none. Kept small: each addition is a
// field nobody can reason about.
const ANY_OWNER = '*';

const PRODUCT_FIELD_OWNERS = {
  product_id: 'catalog-service',
  // Whose product this is. Catalog-owned: a seller cannot be reassigned by
  // a price change or a stock movement.
  seller_id: 'catalog-service',
  sku: 'catalog-service',
  name: 'catalog-service',
  description: 'catalog-service',
  is_active: 'catalog-service',
  // catalog's list price. Distinct from price_cents on purpose -- one field
  // with two claimants is what made the price ambiguous for the life of the
  // project.
  base_price_cents: 'catalog-service',
  // Single-owner timestamp, so a backfill can tell its own writes apart from
  // everyone else's.
  catalog_updated_at: 'catalog-service',

  // The effective price a customer pays (§3: pricing owns pricing).
  price_cents: 'pricing-service',

  quantity_available: 'inventory-service',

  // --- seller signals, denormalised (ARCHITECTURE 3g) ---------------------
  // Facts about the seller, copied here so a search scores in one query
  // instead of N HTTP lookups. Refreshed as a set: a partial write would
  // leave today's rating beside last week's return rate and nothing would
  // look wrong.
  seller_rating: 'seller-projection',
  seller_review_count: 'seller-projection',
  seller_on_time_dispatch_rate: 'seller-projection',
  seller_cancellation_rate: 'seller-projection',
  seller_return_rate: 'seller-projection',
  // How much fulfilment history backs those rates. A rate without its
  // confidence is a number that looks more certain than it is.
  seller_confidence: 'seller-projection',
  // geo_point. Absent means unlocated rather than far away -- distance_decay
  // returns exactly 1.0 for a missing distance, so a seller who never set
  // coordinates is not penalised for it.
  seller_location: 'seller-projection',
  seller_signals_updated_at: 'seller-projection',

  updated_at: '*'
};

const KNOWN_OWNERS = [CATALOG, PRICING, INVENTORY, SELLER_SIGNALS];

class OwnershipError extends Error {
  constructor(message) {
    super(message);
    this.name = 'OwnershipError';
  }
}

/** Every field `owner` may write, including the shared ones. */
function fieldsOwnedBy(owner) {
  return Object.keys(PRODUCT_FIELD_OWNERS).filter(
    (field) => PRODUCT_FIELD_OWNERS[field] === owner
      || PRODUCT_FIELD_OWNERS[field] === ANY_OWNER
  );
}

/** Fields belonging to somebody else -- the ones a bug would erase. */
function foreignFields(owner) {
  return Object.keys(PRODUCT_FIELD_OWNERS).filter(
    (field) => PRODUCT_FIELD_OWNERS[field] !== owner
      && PRODUCT_FIELD_OWNERS[field] !== ANY_OWNER
  );
}

/**
 * Throw unless `owner` may write every one of `fields`.
 *
 * Unknown fields are refused as well as foreign ones. Elasticsearch maps new
 * fields dynamically, so a typo does not fail -- it silently creates
 * `quantity_avaliable` beside the real one and nothing looks wrong until a
 * search returns nothing.
 */
function validateProductWrite(owner, fields) {
  if (!KNOWN_OWNERS.includes(owner)) {
    throw new OwnershipError(
      `unknown writer '${owner}'; expected one of ${KNOWN_OWNERS.sort().join(', ')}`
    );
  }

  const allowed = new Set(fieldsOwnedBy(owner));
  const offending = Array.from(fields).filter((f) => !allowed.has(f));
  if (offending.length === 0) return;

  const stolen = offending.filter((f) => f in PRODUCT_FIELD_OWNERS).sort();
  const unknown = offending.filter((f) => !(f in PRODUCT_FIELD_OWNERS)).sort();

  const parts = [];
  if (stolen.length) {
    parts.push('fields owned by another service: '
      + stolen.map((f) => `${f} (${PRODUCT_FIELD_OWNERS[f]})`).join(', '));
  }
  if (unknown.length) {
    parts.push(`fields nobody owns: ${unknown.join(', ')}`);
  }

  throw new OwnershipError(
    `${owner} may not write ${parts.join('; ')}. Writing another service's `
    + 'field into this document overwrites live data that this service cannot '
    + 'reconstruct.'
  );
}

/**
 * The Elasticsearch body for a partial product write.
 *
 * Always doc_as_upsert, never a whole document. The upsert half matters for
 * ordering: a price arriving before its ProductCreated should keep the price
 * rather than be dropped, and it becomes visible once catalog catches up --
 * search requires the catalog marker, so a partial document is never served as
 * a product.
 */
function productUpdateBody(owner, fields) {
  validateProductWrite(owner, Object.keys(fields));
  return { doc: { ...fields }, doc_as_upsert: true };
}

/**
 * The only supported way to write a product document.
 *
 * `es` is any client exposing update({index, id, body}). Deliberately no
 * counterpart that replaces a document: if one existed it would eventually be
 * used, which is the entire history of this file.
 */
function writeProduct(es, productId, owner, fields, index = PRODUCTS_INDEX) {
  const body = productUpdateBody(owner, fields);
  // productId is the document id, so it costs nothing to keep it in the source
  // too. Upserts that omitted it left documents whose whole content was a stock
  // level and a timestamp.
  if (body.doc.product_id === undefined) body.doc.product_id = productId;
  return es.update({ index, id: productId, body });
}

module.exports = {
  PRODUCTS_INDEX,
  CATALOG,
  PRICING,
  INVENTORY,
  SELLER_SIGNALS,
  ANY_OWNER,
  PRODUCT_FIELD_OWNERS,
  OwnershipError,
  fieldsOwnedBy,
  foreignFields,
  validateProductWrite,
  productUpdateBody,
  writeProduct
};
