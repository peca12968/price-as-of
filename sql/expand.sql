ALTER TABLE tickets ADD COLUMN pick_slot text;

-- While historical rows are incomplete, expose their legacy value to new clients.
CREATE VIEW tickets_v2 AS SELECT id, bin_code AS pick_slot FROM tickets;

CREATE FUNCTION sync_slot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.bin_code := coalesce(NEW.bin_code, NEW.pick_slot);
        NEW.pick_slot := coalesce(NEW.pick_slot, NEW.bin_code);
    ELSIF NEW.bin_code IS DISTINCT FROM OLD.bin_code
      AND NEW.pick_slot IS NOT DISTINCT FROM OLD.pick_slot THEN
        NEW.pick_slot := NEW.bin_code;
    ELSIF NEW.pick_slot IS DISTINCT FROM OLD.pick_slot
      AND NEW.bin_code IS NOT DISTINCT FROM OLD.bin_code THEN
        NEW.bin_code := NEW.pick_slot;
    ELSIF NEW.pick_slot IS NULL AND OLD.pick_slot IS NULL THEN
        NEW.pick_slot := NEW.bin_code;
    END IF;
    IF NEW.bin_code IS NULL OR NEW.pick_slot IS DISTINCT FROM NEW.bin_code THEN
        RAISE EXCEPTION 'slot values must agree and be non-null' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER sync_slot BEFORE INSERT OR UPDATE ON tickets
FOR EACH ROW EXECUTE FUNCTION sync_slot();

ALTER TABLE tickets ADD CONSTRAINT slots_match
    CHECK (pick_slot IS NOT NULL AND pick_slot = bin_code) NOT VALID;
