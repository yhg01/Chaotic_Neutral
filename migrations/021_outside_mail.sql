-- Mail an assistant sends to someone who is not a participant of a clocked run. It has no
-- mailbox here, so it stays in the sender's sent mail and the recipient column stays empty.
-- Delivered mail keeps its foreign key to run_participants.
ALTER TABLE inbox_messages
    ADD COLUMN outside_recipient text
    CHECK (outside_recipient IS NULL OR length(outside_recipient) BETWEEN 1 AND 160);
ALTER TABLE inbox_messages ALTER COLUMN recipient_owner_id DROP NOT NULL;
ALTER TABLE inbox_messages
    ADD CONSTRAINT inbox_one_recipient
    CHECK ((recipient_owner_id IS NULL) = (outside_recipient IS NOT NULL));
ALTER TABLE inbox_messages
    ADD CONSTRAINT inbox_outside_from_assistant
    CHECK (outside_recipient IS NULL OR sender_source = 'agent');
