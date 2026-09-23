-- Esquema del simulador de costos de dotación.
-- Fechas en texto ISO (AAAA-MM-DD), dinero en pesos enteros, horas en minutos
-- enteros y porcentajes en puntos básicos (1525 = 15,25 %).

CREATE TABLE site (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE program (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

-- Estructura financiera del convenio: monto total, vigencia y una referencia de texto libre
-- (por ejemplo el número de resolución; solo tiene sentido en la base de datos real del usuario).
CREATE TABLE program_budget (
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    start_date TEXT CHECK (start_date IS NULL OR date(start_date) IS start_date),
    end_date TEXT CHECK (end_date IS NULL OR (date(end_date) IS end_date AND (start_date IS NULL OR end_date >= start_date))),
    reference TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (program_id, year)
);

-- Ítem presupuestario del convenio: recurso humano, operación, inversión u otro.
CREATE TABLE budget_item (
    id INTEGER PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    code TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    item_type TEXT NOT NULL CHECK (item_type IN ('human_resources', 'operation', 'investment', 'other')),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    UNIQUE (program_id, year, code)
);

CREATE INDEX budget_item_program_year_idx ON budget_item(program_id, year);

CREATE TABLE job_role (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category TEXT NOT NULL CHECK (category IN ('A', 'B', 'C', 'D', 'E', 'F'))
);

-- Valor hora de honorarios por categoría y año, o específico de un cargo y año (exactamente uno
-- de los dos). El usuario lo digita cada año (reajuste propio del municipio).
CREATE TABLE rate (
    id INTEGER PRIMARY KEY,
    category TEXT CHECK (category IN ('A', 'B', 'C', 'D', 'E', 'F')),
    job_role_id INTEGER REFERENCES job_role(id) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    hourly_rate INTEGER NOT NULL CHECK (hourly_rate > 0),
    CHECK ((category IS NULL) <> (job_role_id IS NULL)),
    UNIQUE (category, year),
    UNIQUE (job_role_id, year)
);

-- Sueldo base del grado 15 por categoría, monto mensual para la jornada completa (44 h),
-- vigente desde una fecha. Los reajustes del sector público se aplican sobre este monto.
CREATE TABLE salary_scale (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL CHECK (category IN ('A', 'B', 'C', 'D', 'E', 'F')),
    valid_from TEXT NOT NULL CHECK (date(valid_from) IS valid_from),
    monthly_amount INTEGER NOT NULL CHECK (monthly_amount > 0),
    UNIQUE (category, valid_from)
);

-- Reajuste del sector público: aplica solo a plazo fijo y planta, de forma multiplicativa, a los
-- meses desde su fecha de vigencia, sobre escalas cuya fecha de vigencia sea anterior a la suya.
CREATE TABLE salary_adjustment (
    valid_from TEXT PRIMARY KEY CHECK (date(valid_from) IS valid_from),
    percent_bp INTEGER NOT NULL CHECK (percent_bp BETWEEN -10000 AND 10000),
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE contract_type (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    cost_method TEXT NOT NULL CHECK (cost_method IN ('weekly_fee', 'hourly_fee', 'salaried')),
    applies_retention INTEGER NOT NULL CHECK (applies_retention IN (0, 1)),
    CHECK (cost_method <> 'salaried' OR applies_retention = 0)
);

-- Aporte del empleador por tipo de contrato dependiente y año (0 % por defecto).
CREATE TABLE employer_contribution (
    contract_type_id INTEGER NOT NULL REFERENCES contract_type(id) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    rate_bp INTEGER NOT NULL CHECK (rate_bp BETWEEN 0 AND 10000),
    PRIMARY KEY (contract_type_id, year)
);

-- Tasa de retención de honorarios vigente desde el año indicado.
CREATE TABLE retention_rate (
    year INTEGER PRIMARY KEY CHECK (year BETWEEN 2000 AND 2100),
    rate_bp INTEGER NOT NULL CHECK (rate_bp BETWEEN 0 AND 10000)
);

CREATE TABLE setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE person (
    id INTEGER PRIMARY KEY,
    rut TEXT UNIQUE,
    full_name TEXT NOT NULL CHECK (length(trim(full_name)) > 0)
);

CREATE TABLE scenario (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) > 0),
    description TEXT NOT NULL DEFAULT '',
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    partial_month_method TEXT NOT NULL CHECK (partial_month_method IN ('proportional', 'full_month')),
    expected_absence_bp INTEGER NOT NULL DEFAULT 0 CHECK (expected_absence_bp BETWEEN 0 AND 5000),
    base_scenario_id INTEGER REFERENCES scenario(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (base_scenario_id IS NULL OR base_scenario_id <> id)
);

-- grade: grado real de la persona, informativo (solo dependientes); el costo siempre usa el
-- grado 15 y la diferencia la asume el municipio, fuera del programa.
CREATE TABLE position (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    job_role_id INTEGER NOT NULL REFERENCES job_role(id) ON DELETE RESTRICT,
    contract_type_id INTEGER NOT NULL REFERENCES contract_type(id) ON DELETE RESTRICT,
    site_id INTEGER NOT NULL REFERENCES site(id) ON DELETE RESTRICT,
    program_id INTEGER NOT NULL REFERENCES program(id) ON DELETE RESTRICT,
    budget_item_id INTEGER NOT NULL REFERENCES budget_item(id) ON DELETE RESTRICT,
    person_id INTEGER REFERENCES person(id) ON DELETE RESTRICT,
    weekly_minutes INTEGER CHECK (weekly_minutes IS NULL OR weekly_minutes BETWEEN 1 AND 2880),
    monthly_minutes INTEGER CHECK (monthly_minutes IS NULL OR monthly_minutes BETWEEN 1 AND 13200),
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity BETWEEN 1 AND 100),
    start_date TEXT NOT NULL CHECK (date(start_date) IS start_date),
    end_date TEXT CHECK (end_date IS NULL OR (date(end_date) IS end_date AND end_date >= start_date)),
    grade INTEGER CHECK (grade IS NULL OR grade BETWEEN 1 AND 30),
    note TEXT NOT NULL DEFAULT '',
    CHECK ((weekly_minutes IS NULL) <> (monthly_minutes IS NULL)),
    CHECK (person_id IS NULL OR quantity = 1)
);

CREATE INDEX position_scenario_idx ON position(scenario_id);
CREATE INDEX position_person_idx ON position(person_id);
CREATE INDEX position_budget_item_idx ON position(budget_item_id);

-- Otro gasto planificado del escenario (arriendos, compras, insumos, capacitación, etc.),
-- imputado a un ítem presupuestario. Único: se paga entero en el mes de start_date. Mensual
-- recurrente: se paga entero cada mes entre start_date y end_date (vacío = hasta fin de año).
CREATE TABLE other_expense (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    budget_item_id INTEGER NOT NULL REFERENCES budget_item(id) ON DELETE RESTRICT,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    expense_type TEXT NOT NULL CHECK (expense_type IN ('monthly', 'one_time')),
    start_date TEXT NOT NULL CHECK (date(start_date) IS start_date),
    end_date TEXT CHECK (end_date IS NULL OR (date(end_date) IS end_date AND end_date >= start_date)),
    amount INTEGER NOT NULL CHECK (amount > 0),
    CHECK (expense_type <> 'one_time' OR end_date IS NULL)
);

CREATE INDEX other_expense_scenario_idx ON other_expense(scenario_id);

-- Monto ejecutado (pagado) por ítem presupuestario y mes (el programa y el año se obtienen del
-- ítem). La importación de la plantilla antigua, por programa y mes, se imputa a un ítem por defecto.
CREATE TABLE execution (
    budget_item_id INTEGER NOT NULL REFERENCES budget_item(id) ON DELETE CASCADE,
    month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (budget_item_id, month)
);
