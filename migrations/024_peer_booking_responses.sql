ALTER TABLE booking_inquiries
    ADD COLUMN thread_id uuid REFERENCES messages(id),
    ADD COLUMN response jsonb,
    ADD CONSTRAINT booking_peer_response CHECK (
        (thread_id IS NULL AND response IS NULL)
        OR (thread_id IS NOT NULL AND response IS NOT NULL)
    );
