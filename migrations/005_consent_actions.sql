-- An owner's reservation grant does not permit cancellation of transport.
ALTER TABLE world_accounts ADD COLUMN consent_kinds text[] NOT NULL DEFAULT '{}'
    CHECK (consent_kinds <@ ARRAY['reserve', 'cancel']::text[]);
-- Preserve existing reservation permissions; cancellation now needs an explicit grant.
UPDATE world_accounts SET consent_kinds = ARRAY['reserve']::text[]
    WHERE cardinality(consent_assets) > 0;
