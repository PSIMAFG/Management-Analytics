-- Esquema de Optibox. Tiempos en minutos desde medianoche, fechas en texto ISO.
-- Días de la semana: 0 = lunes ... 4 = viernes.

CREATE TABLE setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE role (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    delivers_services INTEGER NOT NULL CHECK (delivers_services IN (0, 1))
);

CREATE TABLE staff (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role_id INTEGER NOT NULL REFERENCES role(id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE contract (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    valid_from TEXT NOT NULL CHECK (date(valid_from) IS valid_from),
    valid_to TEXT CHECK (valid_to IS NULL OR (date(valid_to) IS valid_to AND valid_to >= valid_from)),
    weekly_minutes INTEGER NOT NULL CHECK (weekly_minutes > 0)
);

-- Las vigencias de los contratos de una persona no pueden solaparse.
CREATE TRIGGER contract_no_overlap_insert
BEFORE INSERT ON contract
WHEN EXISTS (
    SELECT 1 FROM contract c
    WHERE c.staff_id = NEW.staff_id
      AND c.valid_from <= COALESCE(NEW.valid_to, '9999-12-31')
      AND NEW.valid_from <= COALESCE(c.valid_to, '9999-12-31')
)
BEGIN
    SELECT RAISE(ABORT, 'contrato con vigencia solapada');
END;

CREATE TRIGGER contract_no_overlap_update
BEFORE UPDATE ON contract
WHEN EXISTS (
    SELECT 1 FROM contract c
    WHERE c.staff_id = NEW.staff_id
      AND c.id <> NEW.id
      AND c.valid_from <= COALESCE(NEW.valid_to, '9999-12-31')
      AND NEW.valid_from <= COALESCE(c.valid_to, '9999-12-31')
)
BEGIN
    SELECT RAISE(ABORT, 'contrato con vigencia solapada');
END;

CREATE TABLE room (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('box', 'multiuso', 'evaluacion', 'gimnasio', 'grupal', 'psicosocial', 'administrativa')),
    allows_group INTEGER NOT NULL CHECK (allows_group IN (0, 1)),
    allows_evaluation INTEGER NOT NULL CHECK (allows_evaluation IN (0, 1)),
    capacity INTEGER NOT NULL CHECK (capacity >= 1),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    reserved_role_id INTEGER REFERENCES role(id) ON DELETE SET NULL,
    simultaneous_hard INTEGER CHECK (simultaneous_hard IS NULL OR simultaneous_hard >= 1),
    simultaneous_soft INTEGER CHECK (simultaneous_soft IS NULL OR simultaneous_soft >= 1),
    CHECK (kind <> 'administrativa' OR simultaneous_hard IS NOT NULL),
    CHECK (simultaneous_soft IS NULL OR simultaneous_soft <= simultaneous_hard)
);

CREATE TABLE service_type (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    duration_min INTEGER NOT NULL CHECK (duration_min > 0 AND duration_min % 15 = 0),
    participants INTEGER NOT NULL CHECK (participants >= 1),
    requires_group_room INTEGER NOT NULL CHECK (requires_group_room IN (0, 1)),
    requires_evaluation_room INTEGER NOT NULL CHECK (requires_evaluation_room IN (0, 1)),
    admin_minutes INTEGER NOT NULL CHECK (admin_minutes >= 0 AND admin_minutes % 15 = 0),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 3),
    color TEXT NOT NULL,
    max_week_total INTEGER CHECK (max_week_total IS NULL OR max_week_total >= 0),
    max_week_per_staff INTEGER CHECK (max_week_per_staff IS NULL OR max_week_per_staff >= 0),
    max_day_total INTEGER CHECK (max_day_total IS NULL OR max_day_total >= 0),
    max_day_per_staff INTEGER CHECK (max_day_per_staff IS NULL OR max_day_per_staff >= 0)
);

CREATE TABLE role_service (
    role_id INTEGER NOT NULL REFERENCES role(id) ON DELETE CASCADE,
    service_type_id INTEGER NOT NULL REFERENCES service_type(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, service_type_id)
);

CREATE TABLE service_room (
    service_type_id INTEGER NOT NULL REFERENCES service_type(id) ON DELETE CASCADE,
    room_id INTEGER NOT NULL REFERENCES room(id) ON DELETE CASCADE,
    PRIMARY KEY (service_type_id, room_id)
);

-- Lista blanca individual: sin filas, la persona realiza todos los tipos de su cargo.
CREATE TABLE staff_skill (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    service_type_id INTEGER NOT NULL REFERENCES service_type(id) ON DELETE CASCADE,
    PRIMARY KEY (staff_id, service_type_id)
);

-- Metas blandas de minutos semanales por tipo de atención.
CREATE TABLE staff_target (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    service_type_id INTEGER NOT NULL REFERENCES service_type(id) ON DELETE CASCADE,
    min_minutes INTEGER CHECK (min_minutes IS NULL OR min_minutes >= 0),
    max_minutes INTEGER CHECK (max_minutes IS NULL OR max_minutes >= 0),
    PRIMARY KEY (staff_id, service_type_id),
    CHECK (min_minutes IS NOT NULL OR max_minutes IS NOT NULL),
    CHECK (min_minutes IS NULL OR max_minutes IS NULL OR min_minutes <= max_minutes)
);

CREATE TABLE staff_room (
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    room_id INTEGER NOT NULL REFERENCES room(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('permitida', 'prohibida', 'preferida')),
    rank INTEGER CHECK (rank IS NULL OR rank >= 1),
    PRIMARY KEY (staff_id, room_id),
    CHECK (kind <> 'preferida' OR rank IS NOT NULL)
);

CREATE TABLE availability (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 4),
    start_min INTEGER NOT NULL CHECK (start_min >= 0 AND start_min % 15 = 0),
    end_min INTEGER NOT NULL CHECK (end_min <= 1440 AND end_min % 15 = 0),
    CHECK (start_min < end_min)
);

CREATE TABLE absence (
    id INTEGER PRIMARY KEY,
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    day TEXT NOT NULL CHECK (date(day) IS day),
    start_min INTEGER CHECK (start_min IS NULL OR start_min >= 0),
    end_min INTEGER CHECK (end_min IS NULL OR end_min <= 1440),
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('aprobada', 'pendiente', 'rechazada')),
    CHECK ((start_min IS NULL) = (end_min IS NULL)),
    CHECK (start_min IS NULL OR start_min < end_min)
);

CREATE TABLE holiday (
    day TEXT PRIMARY KEY CHECK (date(day) IS day),
    name TEXT NOT NULL
);

CREATE TABLE center_hours (
    weekday INTEGER PRIMARY KEY CHECK (weekday BETWEEN 0 AND 4),
    open_min INTEGER NOT NULL CHECK (open_min % 15 = 0),
    close_min INTEGER NOT NULL CHECK (close_min % 15 = 0 AND close_min <= 1440),
    lunch_start_min INTEGER NOT NULL CHECK (lunch_start_min % 15 = 0),
    lunch_end_min INTEGER NOT NULL CHECK (lunch_end_min % 15 = 0),
    CHECK (open_min <= lunch_start_min AND lunch_start_min <= lunch_end_min AND lunch_end_min <= close_min),
    CHECK (open_min < close_min)
);

CREATE TABLE blocking (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    weekday INTEGER CHECK (weekday IS NULL OR weekday BETWEEN 0 AND 4),
    start_min INTEGER NOT NULL CHECK (start_min >= 0),
    end_min INTEGER NOT NULL CHECK (end_min <= 1440),
    audience TEXT NOT NULL CHECK (audience IN ('todos', 'asistencial', 'cargo', 'persona')),
    role_id INTEGER REFERENCES role(id) ON DELETE CASCADE,
    staff_id INTEGER REFERENCES staff(id) ON DELETE CASCADE,
    counts_as_admin INTEGER NOT NULL DEFAULT 0 CHECK (counts_as_admin IN (0, 1)),
    CHECK (start_min < end_min),
    CHECK ((audience = 'cargo') = (role_id IS NOT NULL)),
    CHECK ((audience = 'persona') = (staff_id IS NOT NULL))
);

CREATE TABLE demand (
    id INTEGER PRIMARY KEY,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 4),
    block_start_min INTEGER NOT NULL CHECK (block_start_min % 60 = 0),
    service_type_id INTEGER NOT NULL REFERENCES service_type(id) ON DELETE CASCADE,
    sessions INTEGER NOT NULL CHECK (sessions >= 0),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 3),
    UNIQUE (weekday, block_start_min, service_type_id)
);

CREATE TABLE scenario (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    room_preference_weight INTEGER NOT NULL CHECK (room_preference_weight >= 0),
    room_continuity_weight INTEGER NOT NULL CHECK (room_continuity_weight >= 0),
    admin_excess_weight INTEGER NOT NULL CHECK (admin_excess_weight >= 0),
    target_weight INTEGER NOT NULL CHECK (target_weight >= 0),
    mix_weight INTEGER NOT NULL CHECK (mix_weight >= 0),
    coverage_floor_pct INTEGER NOT NULL CHECK (coverage_floor_pct BETWEEN 50 AND 100)
);

CREATE TABLE scenario_weight (
    scenario_id INTEGER NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    service_type_id INTEGER NOT NULL REFERENCES service_type(id) ON DELETE CASCADE,
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0),
    mix_min REAL CHECK (mix_min IS NULL OR mix_min BETWEEN 0 AND 1),
    mix_max REAL CHECK (mix_max IS NULL OR mix_max BETWEEN 0 AND 1),
    PRIMARY KEY (scenario_id, service_type_id),
    CHECK (mix_min IS NULL OR mix_max IS NULL OR mix_min <= mix_max)
);

-- Corridas: parámetros, resultados de cada fase y una copia de la instancia usada.
CREATE TABLE run (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    week_start TEXT NOT NULL CHECK (date(week_start) IS week_start),
    scenario_id INTEGER REFERENCES scenario(id) ON DELETE SET NULL,
    scenario_name TEXT NOT NULL,
    status TEXT NOT NULL,
    time_limit_s REAL NOT NULL CHECK (time_limit_s > 0),
    seed INTEGER NOT NULL,
    workers INTEGER NOT NULL,
    demand_sessions INTEGER NOT NULL,
    covered_sessions INTEGER NOT NULL,
    greedy_covered_sessions INTEGER NOT NULL,
    phase_a_status TEXT NOT NULL,
    phase_a_value INTEGER,
    phase_a_bound INTEGER,
    phase_a_seconds REAL NOT NULL,
    phase_b_status TEXT,
    phase_b_value INTEGER,
    phase_b_seconds REAL,
    greedy_value INTEGER NOT NULL,
    candidates INTEGER NOT NULL,
    variables INTEGER NOT NULL,
    build_seconds REAL NOT NULL,
    total_seconds REAL NOT NULL,
    session_minutes INTEGER NOT NULL,
    room_utilization REAL,
    instance_json TEXT NOT NULL
);

CREATE TABLE assignment (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('sesion', 'administrativo')),
    staff_code TEXT NOT NULL,
    service_code TEXT,
    room_code TEXT,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 4),
    start_min INTEGER NOT NULL CHECK (start_min >= 0),
    duration_min INTEGER NOT NULL CHECK (duration_min > 0),
    participants INTEGER NOT NULL DEFAULT 0 CHECK (participants >= 0),
    CHECK (kind = 'administrativo' OR (service_code IS NOT NULL AND room_code IS NOT NULL))
);

CREATE INDEX assignment_run ON assignment(run_id);

CREATE TABLE unmet_demand (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 4),
    block_start_min INTEGER NOT NULL,
    service_code TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required >= 0),
    covered INTEGER NOT NULL CHECK (covered >= 0),
    cause TEXT NOT NULL,
    rule_code TEXT,
    detail TEXT NOT NULL
);

CREATE TABLE rule_exclusion (
    run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    rule_code TEXT NOT NULL,
    excluded INTEGER NOT NULL CHECK (excluded >= 0),
    PRIMARY KEY (run_id, rule_code)
);
