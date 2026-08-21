-- 019 (payment_ledger_db): the escrow ledger
--
-- Under cash on delivery the platform never touches the buyer's money at
-- checkout. A courier collects it at the door, holds it for days, and remits it
-- in a batch. Between the door and the payout the platform owes a seller money
-- it does not yet have.
--
-- That is a liability, and it belongs in a ledger from the moment of delivery
-- rather than being computed at payout time by summing orders (section 3d). A
-- number derived from orders answers "what do we think we owe" and cannot
-- answer "what did we owe last Tuesday", "why is this seller's balance
-- different from the sum of their orders", or "which of those two numbers is
-- wrong". A ledger answers all three, because every change is an entry with a
-- reason attached.
--
-- Double entry. Debits are positive and credits negative, so a transaction
-- balances when its entries sum to zero, and the whole ledger is consistent
-- when every entry ever written sums to zero. That is one query, and it is the
-- only check that matters.
--
-- Append only. There is no UPDATE path and no DELETE path: a correction is a
-- new transaction that reverses the old one, because a ledger that can be
-- edited is a ledger nobody can testify from.
--
-- The existing `payment_ledger` table is untouched. It records card charges and
-- is a different thing entirely despite the name -- one row per attempt, with a
-- status, not a double-entry book.
--
--   psql -U admin -d payment_ledger_db -f migrations/019_payment_db_escrow_ledger.sql
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS public.ledger_entries (
    id               uuid PRIMARY KEY,

    -- Every entry of one transaction shares this. The balance check is
    -- per-transaction, so without it there is no way to ask whether a
    -- particular event balanced.
    transaction_id   uuid         NOT NULL,

    account          varchar(32)  NOT NULL,

    -- Signed minor units. Positive debit, negative credit. Rule 6: integers,
    -- and the minor unit is the poisha.
    amount_cents     bigint       NOT NULL,
    currency         varchar(3)   NOT NULL DEFAULT 'BDT',

    reason           varchar(32)  NOT NULL,
    seller_id        uuid,
    seller_order_id  uuid,

    -- The commission rate this entry was booked at, in basis points, and the
    -- contract version it came from. Recorded on the entry rather than looked
    -- up later because the ledger is the evidence: a seller disputing their
    -- balance is owed the rate that was actually applied, not whatever the
    -- rate table says today.
    commission_bps   integer,
    contract_version integer,

    detail           varchar(512),
    created_at       timestamp with time zone NOT NULL DEFAULT NOW()
);

-- "Does this transaction balance", and the per-transaction read.
CREATE INDEX IF NOT EXISTS ix_ledger_entries_transaction
    ON public.ledger_entries (transaction_id);

-- A seller's balance, which is the hottest read: every payout run and every
-- seller dashboard asks it.
CREATE INDEX IF NOT EXISTS ix_ledger_entries_seller_account
    ON public.ledger_entries (seller_id, account);

-- "What happened to this order's money", for a dispute.
CREATE INDEX IF NOT EXISTS ix_ledger_entries_seller_order
    ON public.ledger_entries (seller_order_id)
    WHERE seller_order_id IS NOT NULL;

-- One booking per seller order per reason. This is what makes a redelivered
-- delivery event a no-op rather than a second credit to the seller: couriers
-- resend callbacks, and the dispatcher delivers at least once.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_seller_order_reason
    ON public.ledger_entries (seller_order_id, reason, account)
    WHERE seller_order_id IS NOT NULL;
