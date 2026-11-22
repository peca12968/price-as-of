ALTER TABLE tickets VALIDATE CONSTRAINT slots_match;
CREATE OR REPLACE VIEW tickets_v2 AS SELECT id, pick_slot FROM tickets;
