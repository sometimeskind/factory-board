-- A step can happen on none of the four machines: at a tablet, on someone's phone, at the
-- router's console. Such an item's machine is 'other', and its `place` field says where.

ALTER TABLE items DROP CONSTRAINT items_machine_allowed;

ALTER TABLE items ADD
    CONSTRAINT items_machine_allowed
        CHECK (machine IN ('browser', 'phone-vm', 'workstation', 'pilot', 'other'));
