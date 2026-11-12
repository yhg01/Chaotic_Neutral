-- A record that the owners it is shared with can also change, such as a group sheet.
-- Without this setting, only the record's owner side can change it, as before.
ALTER TABLE owner_resources ADD COLUMN shared_can_edit boolean NOT NULL DEFAULT false;
ALTER TABLE owner_resources ADD CONSTRAINT owner_resources_shared_can_edit_check
    CHECK (NOT shared_can_edit OR (visibility = 'shared' AND cardinality(shared_owner_ids) > 0));
