-- Modelo de datos del monitor de indicadores.
-- Las entidades se identifican por código. Las fechas se guardan como texto ISO.

CREATE TABLE program (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE site (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- Población de referencia de cada sede por año (denominador de coberturas poblacionales).
CREATE TABLE site_population (
    site_code TEXT NOT NULL REFERENCES site(code) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    population INTEGER NOT NULL CHECK (population > 0),
    PRIMARY KEY (site_code, year)
);

-- Definición declarativa del indicador y su ficha técnica.
CREATE TABLE indicator (
    code TEXT PRIMARY KEY,
    program_code TEXT NOT NULL REFERENCES program(code),
    name TEXT NOT NULL UNIQUE,
    short_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    aggregation TEXT NOT NULL CHECK (aggregation IN ('flow', 'stock')),
    denominator_type TEXT NOT NULL
        CHECK (denominator_type IN ('flow', 'fixed_site', 'stock', 'k_stock', 'population', 'manual')),
    multiplier REAL NOT NULL DEFAULT 1 CHECK (multiplier > 0),
    direction TEXT NOT NULL CHECK (direction IN ('higher', 'lower')),
    scale TEXT NOT NULL CHECK (scale IN ('proportion', 'rate', 'days')),
    natural_ceiling REAL CHECK (natural_ceiling IS NULL OR natural_ceiling > 0),
    stock_months TEXT NOT NULL DEFAULT '',
    cut_months TEXT NOT NULL DEFAULT '',
    measures TEXT NOT NULL,
    numerator_desc TEXT NOT NULL,
    denominator_desc TEXT NOT NULL,
    source TEXT NOT NULL,
    zero_meaning TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    CHECK (denominator_type = 'k_stock' OR multiplier = 1)
);

-- Regla de meta y peso de cada indicador por año. Sin regla, el indicador no está vigente ese año.
CREATE TABLE goal_rule (
    indicator_code TEXT NOT NULL REFERENCES indicator(code) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    rule_type TEXT NOT NULL
        CHECK (rule_type IN ('absolute', 'relative_increase', 'capped_increase', 'bands', 'baseline')),
    value REAL,
    factor REAL,
    ceiling REAL,
    weight REAL NOT NULL CHECK (weight BETWEEN 0 AND 1),
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (indicator_code, year),
    CHECK (rule_type <> 'absolute' OR (value IS NOT NULL AND value >= 0)),
    CHECK (rule_type NOT IN ('relative_increase', 'capped_increase') OR (factor IS NOT NULL AND factor > 0)),
    CHECK (rule_type <> 'capped_increase' OR (ceiling IS NOT NULL AND ceiling > 0))
);

-- Tramos de cumplimiento (intervalos semiabiertos: cota anterior < valor <= cota).
CREATE TABLE goal_band (
    indicator_code TEXT NOT NULL,
    year INTEGER NOT NULL,
    band_order INTEGER NOT NULL CHECK (band_order >= 1),
    upper_bound REAL,
    compliance REAL NOT NULL CHECK (compliance BETWEEN 0 AND 1),
    PRIMARY KEY (indicator_code, year, band_order),
    FOREIGN KEY (indicator_code, year) REFERENCES goal_rule(indicator_code, year) ON DELETE CASCADE
);

-- Meta fija anual por sede para los indicadores de compromiso fijo.
CREATE TABLE site_goal (
    indicator_code TEXT NOT NULL REFERENCES indicator(code) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    site_code TEXT NOT NULL REFERENCES site(code) ON DELETE CASCADE,
    target REAL NOT NULL CHECK (target > 0),
    PRIMARY KEY (indicator_code, year, site_code)
);

-- Dato mensual por indicador y sede. `reported` = 0 indica un mes faltante (no es cero).
CREATE TABLE observation (
    id INTEGER PRIMARY KEY,
    indicator_code TEXT NOT NULL REFERENCES indicator(code) ON DELETE CASCADE,
    site_code TEXT NOT NULL REFERENCES site(code) ON DELETE CASCADE,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2100),
    month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    numerator REAL CHECK (numerator IS NULL OR numerator >= 0),
    denominator REAL CHECK (denominator IS NULL OR denominator >= 0),
    reported INTEGER NOT NULL DEFAULT 1 CHECK (reported IN (0, 1)),
    origin TEXT NOT NULL CHECK (origin IN ('synthetic', 'imported', 'manual')),
    loaded_at TEXT NOT NULL,
    UNIQUE (indicator_code, site_code, year, month),
    CHECK (reported = 1 OR (numerator IS NULL AND denominator IS NULL))
);

CREATE INDEX ix_observation_year_month ON observation (year, month);
CREATE INDEX ix_observation_site ON observation (site_code, year);

-- Parámetros de cálculo: umbrales del semáforo, prevalencia, período por defecto y bootstrap.
CREATE TABLE setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
