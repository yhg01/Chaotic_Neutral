-- One durable cap covers every provider attempt in a test campaign.
CREATE TABLE model_campaigns (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 160),
    limit_usd numeric(24,12) NOT NULL CHECK (limit_usd > 0),
    halted boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE model_spend_attempts (
    id uuid PRIMARY KEY,
    campaign_id uuid NOT NULL REFERENCES model_campaigns(id),
    run_id uuid REFERENCES environment_runs(id),
    owner_id text,
    role text NOT NULL,
    model text NOT NULL,
    reserved_usd numeric(24,12) NOT NULL CHECK (reserved_usd >= 0),
    reported_usd numeric(24,12) CHECK (reported_usd >= 0),
    status text NOT NULL CHECK (status IN ('reserved','reported','uncertain','breach')),
    prompt_token_bound bigint NOT NULL CHECK (prompt_token_bound >= 0),
    output_token_bound integer NOT NULL CHECK (output_token_bound > 0),
    price_snapshot jsonb NOT NULL,
    generation_id text,
    reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX model_spend_campaign ON model_spend_attempts(campaign_id, created_at);
CREATE INDEX model_spend_run ON model_spend_attempts(run_id, created_at);
