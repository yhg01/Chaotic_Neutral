-- Future contracts stay private until their declared Clock cue is delivered.
ALTER TABLE confirmation_tasks ADD COLUMN activation_event_key text;
