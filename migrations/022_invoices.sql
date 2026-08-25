-- Monthly per organization. total is stored, not derived from
-- SUM(invoice_lines.amount) at read time — deliberate, and inconsistent
-- with the rest of this codebase's "always derive, never store" convention
-- (see account_summary()) on purpose: an issued invoice is a frozen
-- financial document and must not change if a line is later corrected.
CREATE TABLE IF NOT EXISTS invoices (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    period_start      date NOT NULL,
    period_end        date NOT NULL,
    issued_at         timestamptz,
    due_at            timestamptz,
    status            text NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','issued','paid','overdue')),
    total             numeric(12,2) NOT NULL DEFAULT 0,
    stripe_invoice_id text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invoices_account_period_idx ON invoices (account_id, period_start);
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;

-- The hard rule lives in amount's CHECK: every line is a positive charge —
-- subscription fee owed, cash-recovery commission owed, or credit-recovery
-- commission owed. There is no line shape for "cash refunded" at all,
-- because that money never touches TD's books (Duffel refunds the
-- company's card directly). With no negative line possible and
-- total = SUM(invoice_lines.amount) computed at generation time, a credit
-- line cannot net against a cash line — there is nothing to subtract.
-- line_type distinguishes commission_cash from commission_credit for
-- reconciliation/reporting only; both always add to what's owed.
CREATE TABLE IF NOT EXISTS invoice_lines (
    id               bigserial PRIMARY KEY,
    invoice_id       uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_type        text NOT NULL CHECK (line_type IN
                     ('subscription','commission_cash','commission_credit')),
    description      text NOT NULL DEFAULT '',
    basis_amount     numeric(12,2),
    rate             numeric(5,4),
    amount           numeric(12,2) NOT NULL CHECK (amount >= 0),
    savings_event_id bigint REFERENCES savings_events(id) ON DELETE SET NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS invoice_lines_invoice_idx ON invoice_lines (invoice_id);
ALTER TABLE invoice_lines ENABLE ROW LEVEL SECURITY;
