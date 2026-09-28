-- cap-factors seed data.
--
-- Emission factors are real, sourced values -- not placeholders. The demo loses
-- credibility if someone who knows the domain checks a number and finds nonsense.
-- Every row carries its source. Values are 2024/2025 published figures.
--
-- Sources:
--   DEFRA  = UK Govt GHG Conversion Factors for Company Reporting 2024
--   CEA    = Central Electricity Authority (India) CO2 Baseline Database v20
--   eGRID  = US EPA eGRID2022 subregion output emission rates
--   EEA    = European Environment Agency, grid intensity 2023
--   SCARB  = Scarborough et al. (2014), Climatic Change 125:179-192, dietary GHG
--   IPCC   = IPCC AR6 WG3, aviation radiative forcing uplift

DROP TABLE IF EXISTS emission_factors;
DROP TABLE IF EXISTS regions;
DROP TABLE IF EXISTS dataset_meta;

CREATE TABLE dataset_meta (
    dataset_version TEXT PRIMARY KEY,
    published_at    DATE NOT NULL,
    notes           TEXT
);

CREATE TABLE regions (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    country         TEXT NOT NULL,
    -- Territorial per-capita annual footprint, tCO2e/person/year.
    -- Used by the product for the "compared to your region" figure.
    avg_annual_tco2e NUMERIC(6,2) NOT NULL,
    avg_source      TEXT NOT NULL
);

CREATE TABLE emission_factors (
    id              SERIAL PRIMARY KEY,
    region_code     TEXT NOT NULL REFERENCES regions(code),
    activity        TEXT NOT NULL,
    year            INTEGER NOT NULL,
    -- factor is NOT NULL by design. The cap-factors contract states factors are
    -- always a number >= 0. A null here would be a contract violation, which is
    -- exactly what the null_factor_new_region demo flag simulates at the API
    -- layer -- it does NOT corrupt this table.
    factor          NUMERIC(12,6) NOT NULL CHECK (factor >= 0),
    unit            TEXT NOT NULL,
    source          TEXT NOT NULL,
    dataset_version TEXT NOT NULL REFERENCES dataset_meta(dataset_version),
    UNIQUE (region_code, activity, year)
);

CREATE INDEX idx_factors_lookup ON emission_factors (region_code, activity, year);

INSERT INTO dataset_meta VALUES
    ('2026.1', '2026-01-15', 'Baseline demo dataset. DEFRA 2024 + CEA v20 + eGRID2022.');

-- ---------------------------------------------------------------------------
-- Regions. Six, spanning a wide range of grid intensity so the region dropdown
-- produces visibly different results -- France (nuclear) to Maharashtra (coal)
-- is a ~15x spread on electricity, which makes the demo's numbers move.
-- ---------------------------------------------------------------------------
INSERT INTO regions (code, name, country, avg_annual_tco2e, avg_source) VALUES
    ('IN-KA', 'Karnataka',      'India',         2.10, 'Global Carbon Budget 2024, India territorial per-capita'),
    ('IN-MH', 'Maharashtra',    'India',         2.10, 'Global Carbon Budget 2024, India territorial per-capita'),
    ('GB',    'United Kingdom', 'United Kingdom',4.70, 'Global Carbon Budget 2024, UK territorial per-capita'),
    ('US-CA', 'California',     'United States',14.30, 'Global Carbon Budget 2024, US territorial per-capita'),
    ('DE',    'Germany',        'Germany',       7.90, 'Global Carbon Budget 2024, DE territorial per-capita'),
    ('FR',    'France',         'France',        4.60, 'Global Carbon Budget 2024, FR territorial per-capita');

-- ---------------------------------------------------------------------------
-- Electricity grid intensity, kgCO2e/kWh. The single most region-dependent
-- factor, and the one the wrong_grid_factor scenario (6.3) targets.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version) VALUES
    ('IN-KA', 'electricity_grid', 2026, 0.710000, 'kgCO2e/kWh', 'CEA CO2 Baseline Database v20', '2026.1'),
    ('IN-MH', 'electricity_grid', 2026, 0.820000, 'kgCO2e/kWh', 'CEA CO2 Baseline Database v20', '2026.1'),
    ('GB',    'electricity_grid', 2026, 0.207000, 'kgCO2e/kWh', 'DEFRA 2024, UK grid average',   '2026.1'),
    ('US-CA', 'electricity_grid', 2026, 0.240000, 'kgCO2e/kWh', 'EPA eGRID2022, CAMX subregion', '2026.1'),
    ('DE',    'electricity_grid', 2026, 0.380000, 'kgCO2e/kWh', 'EEA grid intensity 2023, DE',   '2026.1'),
    ('FR',    'electricity_grid', 2026, 0.056000, 'kgCO2e/kWh', 'EEA grid intensity 2023, FR',   '2026.1');

-- ---------------------------------------------------------------------------
-- Heating fuels. Combustion factors, region-invariant (chemistry does not care
-- where you are). Seeded per region anyway so bulk lookup has one code path.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version)
SELECT code, 'natural_gas',  2026, 2.020000, 'kgCO2e/m3',    'DEFRA 2024, natural gas gross CV',   '2026.1' FROM regions
UNION ALL
SELECT code, 'lpg',          2026, 2.939000, 'kgCO2e/kg',    'DEFRA 2024, LPG',                    '2026.1' FROM regions
UNION ALL
SELECT code, 'heating_oil',  2026, 2.540000, 'kgCO2e/litre', 'DEFRA 2024, burning oil',            '2026.1' FROM regions;

-- ---------------------------------------------------------------------------
-- Road travel, kgCO2e/km. Tailpipe + upstream, average vehicle size.
-- Electric car is derived per region: 0.19 kWh/km * that region's grid factor.
-- This is why an EV in France (0.011) and an EV in Maharashtra (0.156) are not
-- the same number -- and it is the kind of detail that makes the demo credible.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version)
SELECT code, 'petrol_car',   2026, 0.170000, 'kgCO2e/km', 'DEFRA 2024, average petrol car',  '2026.1' FROM regions
UNION ALL
SELECT code, 'diesel_car',   2026, 0.168000, 'kgCO2e/km', 'DEFRA 2024, average diesel car',  '2026.1' FROM regions
UNION ALL
SELECT code, 'bus',          2026, 0.102000, 'kgCO2e/km', 'DEFRA 2024, average local bus',   '2026.1' FROM regions
UNION ALL
SELECT code, 'two_wheeler',  2026, 0.114000, 'kgCO2e/km', 'DEFRA 2024, average motorcycle',  '2026.1' FROM regions;

-- EV factor derived from each region's own grid intensity: 0.19 kWh/km.
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version)
SELECT r.code, 'electric_car', 2026,
       ROUND(0.19 * f.factor, 6),
       'kgCO2e/km',
       'Derived: 0.19 kWh/km (DEFRA 2024 average BEV) x regional grid intensity',
       '2026.1'
FROM regions r
JOIN emission_factors f ON f.region_code = r.code AND f.activity = 'electricity_grid';

-- ---------------------------------------------------------------------------
-- Rail, kgCO2e/passenger-km. Genuinely region-dependent: Indian Railways is
-- largely electrified on a coal-heavy grid but runs very high occupancy.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version) VALUES
    ('IN-KA', 'rail', 2026, 0.021000, 'kgCO2e/passenger-km', 'Indian Railways sustainability report 2023', '2026.1'),
    ('IN-MH', 'rail', 2026, 0.021000, 'kgCO2e/passenger-km', 'Indian Railways sustainability report 2023', '2026.1'),
    ('GB',    'rail', 2026, 0.035000, 'kgCO2e/passenger-km', 'DEFRA 2024, national rail',                  '2026.1'),
    ('US-CA', 'rail', 2026, 0.043000, 'kgCO2e/passenger-km', 'EPA / Amtrak intercity average',             '2026.1'),
    ('DE',    'rail', 2026, 0.029000, 'kgCO2e/passenger-km', 'UBA Germany, long-distance rail 2023',       '2026.1'),
    ('FR',    'rail', 2026, 0.006000, 'kgCO2e/passenger-km', 'ADEME Base Carbone, TGV',                    '2026.1');

-- ---------------------------------------------------------------------------
-- Air travel, kgCO2e per ONE-WAY flight, economy, including radiative forcing
-- uplift (x1.9 per IPCC AR6). Haul bands: short <1500km (avg 1100km),
-- medium 1500-4000km (avg 2500km), long >4000km (avg 7000km).
-- Aviation factors do not vary by residence region.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version)
SELECT code, 'flight_short_haul',  2026,  185.000000, 'kgCO2e/flight', 'DEFRA 2024 short-haul economy x1.9 RF uplift (IPCC AR6), 1100km',  '2026.1' FROM regions
UNION ALL
SELECT code, 'flight_medium_haul', 2026,  383.000000, 'kgCO2e/flight', 'DEFRA 2024 medium-haul economy x1.9 RF uplift (IPCC AR6), 2500km', '2026.1' FROM regions
UNION ALL
SELECT code, 'flight_long_haul',   2026, 1104.000000, 'kgCO2e/flight', 'DEFRA 2024 long-haul economy x1.9 RF uplift (IPCC AR6), 7000km',   '2026.1' FROM regions;

-- ---------------------------------------------------------------------------
-- Diet, kgCO2e per PERSON PER YEAR. Scarborough et al. 2014 daily figures for a
-- 2000 kcal diet, annualised (x365). calc-api divides by 12 for a monthly
-- period -- diet is the one input that is inherently annual.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version)
SELECT code, 'diet_high_meat',   2026, 2624.000000, 'kgCO2e/year', 'Scarborough et al. 2014, >100g meat/day (7.19 kg/day)', '2026.1' FROM regions
UNION ALL
SELECT code, 'diet_medium_meat', 2026, 2055.000000, 'kgCO2e/year', 'Scarborough et al. 2014, 50-99g meat/day (5.63 kg/day)', '2026.1' FROM regions
UNION ALL
SELECT code, 'diet_low_meat',    2026, 1705.000000, 'kgCO2e/year', 'Scarborough et al. 2014, <50g meat/day (4.67 kg/day)',   '2026.1' FROM regions
UNION ALL
SELECT code, 'diet_pescatarian', 2026, 1427.000000, 'kgCO2e/year', 'Scarborough et al. 2014, pescatarian (3.91 kg/day)',     '2026.1' FROM regions
UNION ALL
SELECT code, 'diet_vegetarian',  2026, 1391.000000, 'kgCO2e/year', 'Scarborough et al. 2014, vegetarian (3.81 kg/day)',      '2026.1' FROM regions
UNION ALL
SELECT code, 'diet_vegan',       2026, 1055.000000, 'kgCO2e/year', 'Scarborough et al. 2014, vegan (2.89 kg/day)',           '2026.1' FROM regions;

-- ---------------------------------------------------------------------------
-- Waste, kgCO2e/kg. Landfilled mixed municipal waste is dominated by methane
-- from anaerobic decomposition; recycled material is near-zero net. calc-api
-- blends the two by the user's stated recycling percentage.
-- ---------------------------------------------------------------------------
INSERT INTO emission_factors (region_code, activity, year, factor, unit, source, dataset_version)
SELECT code, 'waste_landfill',  2026, 0.467000, 'kgCO2e/kg', 'DEFRA 2024, mixed municipal waste to landfill', '2026.1' FROM regions
UNION ALL
SELECT code, 'waste_recycled',  2026, 0.021000, 'kgCO2e/kg', 'DEFRA 2024, mixed recycling, closed loop',      '2026.1' FROM regions;
