-- Built site modules for clocked runs. Other runs keep these tables empty.
-- A module is source that was written and checked before its scenario was frozen. The frozen
-- scenario holds the same source and hash, so a run can always be read back from its own rows.

CREATE TABLE web_modules (
    run_id uuid NOT NULL REFERENCES environment_runs(id),
    module_id text NOT NULL CHECK (module_id ~ '^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$'),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    source text NOT NULL CHECK (length(source) BETWEEN 1 AND 40000),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, module_id)
);

-- The live state of one module site: what is public, each person's account, and records with
-- an access list. Every committed change adds one to version.
CREATE TABLE web_module_state (
    run_id uuid NOT NULL,
    site_id text NOT NULL,
    state jsonb NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    PRIMARY KEY (run_id, site_id),
    FOREIGN KEY (run_id, site_id) REFERENCES web_sites(run_id, site_id)
);
