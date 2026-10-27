-- Additive migration for department-scoped assignments and progress.
-- Review and back up the selected database before a database operator applies this file.
-- This file is intentionally not run by application startup or by this change.

ALTER TABLE "user"
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE complaint_department
    ADD COLUMN assigned_staff_id INTEGER,
    ADD COLUMN assigned_by INTEGER,
    ADD COLUMN staff_assigned_date TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN status VARCHAR(30) NOT NULL DEFAULT 'Submitted',
    ADD COLUMN resolution_note TEXT;

ALTER TABLE complaint_assignment
    ADD COLUMN department_id INTEGER;

-- Match the current ORM model; assignment actors may be unknown for legacy
-- rows and are intentionally nullable.
ALTER TABLE complaint_assignment
    ALTER COLUMN assigned_by DROP NOT NULL;

ALTER TABLE complaint_status_history
    ADD COLUMN department_id INTEGER;

ALTER TABLE complaint_department
    ADD CONSTRAINT fk_cd_assigned_staff FOREIGN KEY (assigned_staff_id)
    REFERENCES "user" (id) ON DELETE RESTRICT;
ALTER TABLE complaint_department
    ADD CONSTRAINT fk_cd_assigned_by FOREIGN KEY (assigned_by)
    REFERENCES "user" (id) ON DELETE SET NULL;
ALTER TABLE complaint_department
    ADD CONSTRAINT ck_cd_status CHECK
    (status IN ('Submitted', 'Under Review', 'Assigned', 'In Progress', 'Resolved', 'Ignored'));
ALTER TABLE complaint_assignment
    ADD CONSTRAINT fk_ca_department FOREIGN KEY (department_id)
    REFERENCES department (id) ON DELETE RESTRICT;
ALTER TABLE complaint_status_history
    ADD CONSTRAINT fk_csh_department FOREIGN KEY (department_id)
    REFERENCES department (id) ON DELETE RESTRICT;

CREATE INDEX ix_complaint_department_assigned_staff_id
    ON complaint_department (assigned_staff_id);
CREATE INDEX ix_complaint_assignment_department_id
    ON complaint_assignment (department_id);
CREATE INDEX ix_complaint_status_history_department_id
    ON complaint_status_history (department_id);

-- Create only primary-department state rows. A global legacy status cannot be
-- attributed to secondary departments, whose rows retain the safe default.
INSERT INTO complaint_department (complaint_id, department_id, status, resolution_note)
SELECT c.id, c.department_id,
       CASE WHEN c.status = 'Ignored' THEN COALESCE((
                SELECT sh.old_status FROM complaint_status_history AS sh
                WHERE sh.complaint_id = c.id AND sh.new_status = 'Ignored'
                  AND sh.old_status IN ('Submitted', 'Under Review', 'Assigned', 'In Progress')
                ORDER BY sh.timestamp DESC, sh.id DESC LIMIT 1
            ), 'Submitted')
            WHEN c.status IN ('Submitted', 'Under Review', 'Assigned', 'In Progress', 'Resolved')
            THEN c.status ELSE 'Submitted' END,
       c.resolution_note
FROM complaint AS c
WHERE c.department_id IS NOT NULL
ON CONFLICT (complaint_id, department_id) DO NOTHING;

-- Do not infer historical assignment departments, actors, or event times from
-- mutable complaint/user state. Legacy ComplaintAssignment rows remain unscoped.

CREATE FUNCTION spsfrms_validate_department_assignment_staff()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE staff_role TEXT; staff_department INTEGER; staff_active BOOLEAN;
BEGIN
    IF NEW.assigned_staff_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT role, department_id, is_active
      INTO staff_role, staff_department, staff_active
      FROM "user" WHERE id = NEW.assigned_staff_id FOR SHARE;
    IF NOT FOUND OR staff_role <> 'staff' OR staff_department IS DISTINCT FROM NEW.department_id OR NOT staff_active THEN
        RAISE EXCEPTION 'department assignment requires active staff in the selected department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION spsfrms_validate_assignment_history_staff()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE staff_role TEXT; staff_department INTEGER; staff_active BOOLEAN;
BEGIN
    IF NEW.department_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM complaint AS c
        WHERE c.id = NEW.complaint_id
          AND (c.department_id = NEW.department_id OR EXISTS (
              SELECT 1 FROM complaint_department AS cd
              WHERE cd.complaint_id = c.id AND cd.department_id = NEW.department_id
          ))
    ) THEN
        RAISE EXCEPTION 'assignment history department must be linked to the complaint';
    END IF;
    SELECT role, department_id, is_active
      INTO staff_role, staff_department, staff_active
      FROM "user" WHERE id = NEW.staff_id FOR SHARE;
    IF NOT FOUND OR staff_role <> 'staff' OR staff_department IS DISTINCT FROM NEW.department_id OR NOT staff_active THEN
        RAISE EXCEPTION 'assignment history requires active staff in the selected department';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION spsfrms_validate_department_status_history()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.department_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM complaint AS c
        WHERE c.id = NEW.complaint_id
          AND (c.department_id = NEW.department_id OR EXISTS (
              SELECT 1 FROM complaint_department AS cd
              WHERE cd.complaint_id = c.id AND cd.department_id = NEW.department_id
          ))
    ) THEN
        RAISE EXCEPTION 'status history department must be linked to the complaint';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION spsfrms_preserve_assigned_staff_role()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM complaint_department
        WHERE assigned_staff_id = OLD.id AND status NOT IN ('Resolved', 'Ignored')
    ) OR EXISTS (
        SELECT 1 FROM complaint
        WHERE assigned_staff_id = OLD.id
          AND COALESCE(status, 'Submitted') <> 'Resolved'
    ) THEN
        IF (
        NEW.role IS DISTINCT FROM 'staff'
        OR NEW.department_id IS DISTINCT FROM OLD.department_id
        OR NEW.is_active IS FALSE
        ) THEN
            RAISE EXCEPTION 'reassign open work before changing staff eligibility';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_cd_validate_staff
BEFORE INSERT OR UPDATE OF assigned_staff_id, department_id ON complaint_department
FOR EACH ROW EXECUTE FUNCTION spsfrms_validate_department_assignment_staff();
CREATE TRIGGER trg_ca_validate_staff
BEFORE INSERT OR UPDATE OF staff_id, department_id ON complaint_assignment
FOR EACH ROW EXECUTE FUNCTION spsfrms_validate_assignment_history_staff();
CREATE TRIGGER trg_csh_validate_department
BEFORE INSERT OR UPDATE OF complaint_id, department_id ON complaint_status_history
FOR EACH ROW EXECUTE FUNCTION spsfrms_validate_department_status_history();
CREATE TRIGGER trg_user_preserve_assignment_role
BEFORE UPDATE OF role, department_id, is_active ON "user"
FOR EACH ROW EXECUTE FUNCTION spsfrms_preserve_assigned_staff_role();
