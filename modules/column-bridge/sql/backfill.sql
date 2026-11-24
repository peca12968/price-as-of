WITH batch AS (
    SELECT id FROM tickets WHERE pick_slot IS NULL
    ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED
)
UPDATE tickets AS t SET pick_slot = t.bin_code
FROM batch WHERE t.id = batch.id
RETURNING t.id;
