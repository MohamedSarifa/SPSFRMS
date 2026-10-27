-- SPSFRMS baseline for a fresh PostgreSQL database.
-- This is the pre-0002 application schema. Apply only through migration_runner.py.
-- The runner supplies the transaction and records the checksum/version atomically.

CREATE TABLE department (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    email VARCHAR(120),
    head_id INTEGER
);

CREATE TABLE "user" (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(120) NOT NULL UNIQUE,
    phone VARCHAR(20) NOT NULL,
    password VARCHAR(255) NOT NULL,
    role VARCHAR(20),
    registered_date TIMESTAMP WITHOUT TIME ZONE,
    department_id INTEGER,
    CONSTRAINT user_department_id_fkey
        FOREIGN KEY (department_id) REFERENCES department(id) ON DELETE SET NULL
);

ALTER TABLE department
    ADD CONSTRAINT fk_department_head_user
    FOREIGN KEY (head_id) REFERENCES "user"(id) ON DELETE SET NULL;

CREATE TABLE complaint (
    id SERIAL PRIMARY KEY,
    complaint_text TEXT NOT NULL,
    category VARCHAR(100) NOT NULL,
    location VARCHAR(255) NOT NULL,
    image VARCHAR(255),
    keywords VARCHAR(255),
    priority VARCHAR(20),
    status VARCHAR(30),
    created_date TIMESTAMP WITHOUT TIME ZONE,
    updated_date TIMESTAMP WITHOUT TIME ZONE,
    citizen_id INTEGER NOT NULL,
    department_id INTEGER,
    assigned_staff_id INTEGER,
    resolution_note TEXT,
    CONSTRAINT complaint_citizen_id_fkey
        FOREIGN KEY (citizen_id) REFERENCES "user"(id),
    CONSTRAINT complaint_department_id_fkey
        FOREIGN KEY (department_id) REFERENCES department(id),
    CONSTRAINT complaint_assigned_staff_id_fkey
        FOREIGN KEY (assigned_staff_id) REFERENCES "user"(id) ON DELETE SET NULL
);

CREATE TABLE complaint_department (
    id SERIAL PRIMARY KEY,
    complaint_id INTEGER NOT NULL,
    department_id INTEGER NOT NULL,
    assigned_date TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT uq_complaint_department UNIQUE (complaint_id, department_id),
    CONSTRAINT complaint_department_complaint_id_fkey
        FOREIGN KEY (complaint_id) REFERENCES complaint(id) ON DELETE CASCADE,
    CONSTRAINT complaint_department_department_id_fkey
        FOREIGN KEY (department_id) REFERENCES department(id) ON DELETE CASCADE
);

CREATE TABLE complaint_assignment (
    id SERIAL PRIMARY KEY,
    complaint_id INTEGER NOT NULL,
    staff_id INTEGER NOT NULL,
    assigned_by INTEGER,
    assigned_date TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    remarks TEXT,
    CONSTRAINT complaint_assignment_complaint_id_fkey
        FOREIGN KEY (complaint_id) REFERENCES complaint(id) ON DELETE CASCADE,
    CONSTRAINT complaint_assignment_staff_id_fkey
        FOREIGN KEY (staff_id) REFERENCES "user"(id) ON DELETE RESTRICT,
    CONSTRAINT complaint_assignment_assigned_by_fkey
        FOREIGN KEY (assigned_by) REFERENCES "user"(id) ON DELETE RESTRICT
);

CREATE TABLE complaint_status_history (
    id SERIAL PRIMARY KEY,
    complaint_id INTEGER NOT NULL,
    old_status VARCHAR(30),
    new_status VARCHAR(30) NOT NULL,
    updated_by INTEGER,
    timestamp TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    remarks TEXT,
    CONSTRAINT complaint_status_history_complaint_id_fkey
        FOREIGN KEY (complaint_id) REFERENCES complaint(id) ON DELETE CASCADE,
    CONSTRAINT complaint_status_history_updated_by_fkey
        FOREIGN KEY (updated_by) REFERENCES "user"(id) ON DELETE SET NULL
);

CREATE INDEX ix_user_department_id ON "user" (department_id);
CREATE UNIQUE INDEX uq_department_head_id ON department (head_id) WHERE head_id IS NOT NULL;
CREATE INDEX ix_complaint_assignment_complaint_id ON complaint_assignment (complaint_id);
CREATE INDEX ix_complaint_assignment_staff_id ON complaint_assignment (staff_id);
CREATE INDEX ix_complaint_status_history_complaint_id ON complaint_status_history (complaint_id);

-- Canonical department reference rows used by the current routing map. These
-- are application reference data, not demo accounts or fabricated history.
INSERT INTO department (name, email) VALUES
    ('Drainage', 'drainage@spsfrms.local'),
    ('Electricity', 'electricity@spsfrms.local'),
    ('Fire Department', 'firedepartment@spsfrms.local'),
    ('Police', 'police@spsfrms.local'),
    ('Public Health', 'publichealth@spsfrms.local'),
    ('Road Maintenance', 'roadmaintenance@spsfrms.local'),
    ('Street Light', 'streetlight@spsfrms.local'),
    ('Water Supply', 'watersupply@spsfrms.local'),
    ('Waste Management', 'wastemanagement@spsfrms.local');
