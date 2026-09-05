-- M6 OE-2: persist computed cost on usage rows.
--
-- 08 6: every cost record keeps the price revision that produced it, and
-- auxiliary calls are categorized (compaction/embedding/title/review) so
-- primary and helper spend can be separated. Unknown models have no price
-- (cost_usd NULL) - usage is still recorded faithfully, never a guessed
-- price.
ALTER TABLE trace_usage
    ADD COLUMN price_revision TEXT;
ALTER TABLE trace_usage
    ADD COLUMN cost_usd REAL;
ALTER TABLE trace_usage
    ADD COLUMN category TEXT NOT NULL DEFAULT 'primary';
