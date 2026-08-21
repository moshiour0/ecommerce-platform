# Courier provider mappings

One file per courier. Each maps that courier's own status words onto the
platform's canonical vocabulary in
`services/fulfillment-service/app/services/courier_rules.py`.

The canonical side is fixed and is the platform's business. The provider side
is **that courier's documentation**, and must be copied from it rather than
guessed. A mapping written from memory is a guess that looks like
configuration: it will be reviewed as data, trusted as data, and be wrong.

```json
{
  "provider": "<slug used in URLs and stored on shipments>",
  "display_name": "<what a human calls it>",
  "statuses": {
    "PICKED_UP": ["their word", "their other word"],
    "DELIVERED": ["..."],
    "RETURNING": ["..."],
    "RETURNED": ["..."]
  }
}
```

Matching folds case, hyphens and spaces, so `Picked Up`, `picked-up` and
`PICKED_UP` are one entry. A word may not appear under two canonical statuses —
the loader refuses that rather than letting dictionary order decide what a
parcel does.

## What is validated

`validate_mapping` refuses a file that cannot express `PICKED_UP`, `DELIVERED`,
`RETURNING` or `RETURNED`. Those four are the ones that move stock or money; a
courier whose file omits `DELIVERED` would have every parcel sit dispatched
forever while the seller waits to be paid. `tests/unit/test_courier_rules.py`
runs that check over every file in this directory, so a broken mapping fails
the unit tier rather than a delivery.

Statuses the platform does not need — "arrived at sorting hub", "line haul" —
are simply left out. They describe the courier's operation, not the order's.

## Providers not yet configured

`pathao`, `steadfast`, `redx`, `sundarban` and the rest each need a file here
before they can be used, and each needs somebody to read that provider's API
documentation to write it. They are deliberately absent rather than present
with plausible-looking values: an unmapped courier is refused loudly at
registration, and a wrongly-mapped one silently sends stock back to sellers.

An unknown status from a *configured* courier is not dropped either — it is
recorded on the shipment and parked for a human, because couriers add statuses
without announcing them.
