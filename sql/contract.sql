-- Run after the last v1 client retires. The v2 view must already use pick_slot.
ALTER TABLE tickets ALTER COLUMN pick_slot SET NOT NULL;
DROP TRIGGER sync_slot ON tickets;
ALTER TABLE tickets DROP CONSTRAINT slots_match;
ALTER TABLE tickets DROP COLUMN bin_code RESTRICT;
DROP FUNCTION sync_slot();
