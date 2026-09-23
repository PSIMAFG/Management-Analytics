-- Esquema del lector de boletas de honorarios.
-- La versión se guarda en PRAGMA user_version (ver data/db.py).
-- Fechas en texto ISO (aaaa-mm-dd), períodos como aaaa-mm, dinero en pesos enteros,
-- horas en minutos enteros y tasas en puntos básicos (1450 = 14,50 %).

CREATE TABLE setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE program (
    id INTEGER PRIMARY KEY,
    folder_code TEXT NOT NULL UNIQUE CHECK (length(folder_code) = 3 AND folder_code NOT GLOB '*[^0-9]*'),
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    short_name TEXT NOT NULL UNIQUE CHECK (length(trim(short_name)) > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE program_alias (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    alias TEXT NOT NULL UNIQUE CHECK (length(trim(alias)) > 0),
    priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 0)
);

CREATE TABLE retention_rate (
    year INTEGER PRIMARY KEY CHECK (year BETWEEN 2000 AND 2100),
    rate_bp INTEGER NOT NULL CHECK (rate_bp BETWEEN 0 AND 5000)
);

CREATE TABLE reference_rate (
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    min_hourly INTEGER NOT NULL CHECK (min_hourly > 0),
    max_hourly INTEGER NOT NULL,
    PRIMARY KEY (program_id, year),
    CHECK (max_hourly >= min_hourly)
);

CREATE TABLE provider (
    rut TEXT PRIMARY KEY CHECK (rut GLOB '[1-9]*-[0-9K]'),
    canonical_name TEXT NOT NULL CHECK (length(trim(canonical_name)) > 0),
    confirmed_at TEXT NOT NULL
);

CREATE TABLE batch (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
    files_found INTEGER NOT NULL DEFAULT 0 CHECK (files_found >= 0),
    files_processed INTEGER NOT NULL DEFAULT 0 CHECK (files_processed >= 0),
    files_skipped INTEGER NOT NULL DEFAULT 0 CHECK (files_skipped >= 0)
);

CREATE TABLE source_file (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER REFERENCES batch(id) ON DELETE SET NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    read_error TEXT,
    payment_period TEXT CHECK (payment_period IS NULL OR payment_period GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
    folder_program_code TEXT,
    folio_hint INTEGER,
    service_month_hint INTEGER CHECK (service_month_hint IS NULL OR service_month_hint BETWEEN 1 AND 12),
    duplicate_of INTEGER REFERENCES source_file(id),
    processed_at TEXT NOT NULL,
    UNIQUE (sha256, path)
);

-- Un mismo contenido (hash) puede aparecer en varias rutas, pero solo una es la original.
CREATE UNIQUE INDEX ux_source_file_original ON source_file(sha256) WHERE duplicate_of IS NULL;

CREATE TABLE receipt (
    id INTEGER PRIMARY KEY,
    source_file_id INTEGER NOT NULL REFERENCES source_file(id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL CHECK (page_index >= 0),
    read_status TEXT NOT NULL CHECK (read_status IN ('ok', 'no_receipt', 'unreadable')),
    text_kind TEXT NOT NULL CHECK (text_kind IN ('native', 'ocr', 'none')),
    issuer_rut TEXT,
    issuer_name TEXT,
    receiver_rut TEXT,
    receiver_name TEXT,
    folio INTEGER CHECK (folio IS NULL OR folio > 0),
    issue_date TEXT CHECK (issue_date IS NULL OR issue_date GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'),
    service_period TEXT CHECK (service_period IS NULL OR service_period GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
    payment_period TEXT CHECK (payment_period IS NULL OR payment_period GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]'),
    gross INTEGER CHECK (gross IS NULL OR gross >= 0),
    retention INTEGER CHECK (retention IS NULL OR retention >= 0),
    net INTEGER CHECK (net IS NULL OR net >= 0),
    printed_rate_bp INTEGER CHECK (printed_rate_bp IS NULL OR printed_rate_bp BETWEEN 0 AND 5000),
    program_id INTEGER REFERENCES program(id),
    folder_program_id INTEGER REFERENCES program(id),
    text_program_id INTEGER REFERENCES program(id),
    text_program_ambiguous INTEGER NOT NULL DEFAULT 0 CHECK (text_program_ambiguous IN (0, 1)),
    hours_minutes INTEGER CHECK (hours_minutes IS NULL OR hours_minutes > 0),
    workday_type TEXT CHECK (workday_type IS NULL OR workday_type IN ('semanal', 'mensual')),
    decree_number INTEGER CHECK (decree_number IS NULL OR decree_number > 0),
    decree_year INTEGER CHECK (decree_year IS NULL OR decree_year BETWEEN 2000 AND 2100),
    gloss TEXT,
    ocr_confidence REAL CHECK (ocr_confidence IS NULL OR ocr_confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'corrected', 'discarded', 'error')),
    discard_reason TEXT,
    accepted_at TEXT,
    edited INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_file_id, page_index),
    CHECK (status <> 'discarded' OR length(trim(coalesce(discard_reason, ''))) > 0)
);

-- Dos boletas válidas no pueden compartir emisor y folio.
CREATE UNIQUE INDEX ux_receipt_valid_folio ON receipt(issuer_rut, folio)
    WHERE status IN ('approved', 'corrected') AND issuer_rut IS NOT NULL AND folio IS NOT NULL;
CREATE INDEX ix_receipt_status ON receipt(status);
CREATE INDEX ix_receipt_issuer_folio ON receipt(issuer_rut, folio);
CREATE INDEX ix_receipt_program ON receipt(program_id);

CREATE TABLE receipt_issue (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    field TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('blocking', 'warning')),
    overridable INTEGER NOT NULL CHECK (overridable IN (0, 1)),
    message TEXT NOT NULL
);
CREATE INDEX ix_receipt_issue_receipt ON receipt_issue(receipt_id);
CREATE INDEX ix_receipt_issue_code ON receipt_issue(code);

-- Incidencias aceptables que el usuario dio por revisadas al aprobar (solo esos códigos).
CREATE TABLE receipt_acceptance (
    receipt_id INTEGER NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    PRIMARY KEY (receipt_id, code)
);

CREATE TABLE field_extraction (
    receipt_id INTEGER NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    value TEXT,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    source TEXT NOT NULL CHECK (source IN ('native', 'ocr', 'folder', 'filename', 'derived', 'user')),
    PRIMARY KEY (receipt_id, field)
);

CREATE TABLE correction (
    id INTEGER PRIMARY KEY,
    receipt_id INTEGER NOT NULL REFERENCES receipt(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    corrected_at TEXT NOT NULL
);
CREATE INDEX ix_correction_receipt ON correction(receipt_id);
