-- An explicitly approved partial recovery retains all historic budget occupancy.
-- This is an opening balance, not a fabricated provider request or checkpoint.
CREATE TABLE model_campaign_opening_balances (
    campaign_id uuid PRIMARY KEY REFERENCES model_campaigns(id),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    snapshot jsonb NOT NULL,
    attempts bigint NOT NULL CHECK (attempts > 0),
    reported_usd numeric NOT NULL CHECK (reported_usd >= 0),
    reserved_usd numeric NOT NULL CHECK (reserved_usd >= 0),
    uncertain_usd numeric NOT NULL CHECK (uncertain_usd >= 0),
    committed_usd numeric NOT NULL CHECK (
        committed_usd = reported_usd + reserved_usd + uncertain_usd
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE FUNCTION protect_campaign_opening_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Historical campaign opening balances are immutable';
END;
$$;
CREATE TRIGGER campaign_opening_balance_immutable
BEFORE UPDATE OR DELETE ON model_campaign_opening_balances
FOR EACH ROW EXECUTE FUNCTION protect_campaign_opening_balance();
