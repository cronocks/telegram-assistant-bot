-- Link a ledger entry to a credit card.
--   expense    + credit_card_id  → a purchase charged to the card (still an expense).
--   cc_payment + credit_card_id  → paying off the card statement (NOT an expense).
-- NULL = ordinary cash/bank entry (existing behaviour).
ALTER TABLE ledger_entries ADD COLUMN credit_card_id INTEGER REFERENCES credit_cards(id);

CREATE INDEX idx_ledger_credit_card ON ledger_entries(credit_card_id)
    WHERE credit_card_id IS NOT NULL AND voided_at IS NULL;
