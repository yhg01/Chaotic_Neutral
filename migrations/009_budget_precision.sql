-- Keep provider decimals exactly, including values beyond twelve decimal places.
-- The application still validates finite nonnegative costs before persistence.
ALTER TABLE model_campaigns ALTER COLUMN limit_usd TYPE numeric;
ALTER TABLE model_spend_attempts ALTER COLUMN reserved_usd TYPE numeric;
ALTER TABLE model_spend_attempts ALTER COLUMN reported_usd TYPE numeric;
