/* ============================================================
   NEM gold star schema - DIMENSIONS
   Apply this BEFORE 02_facts.sql (facts have FKs to these).
   All datetimes are AEST (UTC+10), no daylight saving, ever.
   Re-runnable: each table is dropped and rebuilt.
   ============================================================ */

/* ---- dim_region --------------------------------------------
   Grain: one row per NEM region (5 total). Static reference. */
IF OBJECT_ID('dbo.dim_region','U') IS NOT NULL DROP TABLE dbo.dim_region;
CREATE TABLE dbo.dim_region (
    region_id   VARCHAR(10) NOT NULL CONSTRAINT pk_dim_region PRIMARY KEY,
    region_name VARCHAR(50) NOT NULL,
    state_code  VARCHAR(3)  NOT NULL
);
INSERT INTO dbo.dim_region (region_id, region_name, state_code) VALUES
    ('NSW1','New South Wales','NSW'),
    ('QLD1','Queensland','QLD'),
    ('VIC1','Victoria','VIC'),
    ('SA1','South Australia','SA'),
    ('TAS1','Tasmania','TAS');

/* ---- dim_interval ------------------------------------------
   Grain: one row per 5-minute period of the day (288 total).
   interval_key = hour*12 + minute/5  (0..287).
   Peak / Shoulder / Off-peak is a DOCUMENTED modelling choice:
     Peak     17:00-20:00 (evening demand peak)
     Shoulder 07:00-22:00 excluding peak
     Off-peak 22:00-07:00                                       */
IF OBJECT_ID('dbo.dim_interval','U') IS NOT NULL DROP TABLE dbo.dim_interval;
CREATE TABLE dbo.dim_interval (
    interval_key SMALLINT   NOT NULL CONSTRAINT pk_dim_interval PRIMARY KEY,
    time_of_day  CHAR(5)    NOT NULL,   -- 'HH:MM'
    hour_of_day  TINYINT    NOT NULL,
    period       VARCHAR(9) NOT NULL
);
;WITH n AS (
    SELECT 0 AS k
    UNION ALL SELECT k + 1 FROM n WHERE k < 287
)
INSERT INTO dbo.dim_interval (interval_key, time_of_day, hour_of_day, period)
SELECT
    k,
    RIGHT('0' + CAST(k/12 AS VARCHAR(2)), 2) + ':' +
        RIGHT('0' + CAST((k%12)*5 AS VARCHAR(2)), 2),
    k/12,
    CASE
        WHEN k/12 >= 17 AND k/12 < 20 THEN 'Peak'
        WHEN k/12 >= 7  AND k/12 < 22 THEN 'Shoulder'
        ELSE 'Off-peak'
    END
FROM n
OPTION (MAXRECURSION 0);

/* ---- dim_date ----------------------------------------------
   Grain: one row per calendar date.
   date_key = yyyyMMdd (int). Financial year = AU (Jul-Jun),
   labelled by its ENDING year (Jul 2026 - Jun 2027 => 2027).
   is_public_holiday left 0 for now (TODO: load an AU holiday
   source; it's a documented gap, not an oversight).            */
IF OBJECT_ID('dbo.dim_date','U') IS NOT NULL DROP TABLE dbo.dim_date;
CREATE TABLE dbo.dim_date (
    date_key          INT        NOT NULL CONSTRAINT pk_dim_date PRIMARY KEY,
    full_date         DATE       NOT NULL,
    [year]            SMALLINT   NOT NULL,
    [quarter]         TINYINT    NOT NULL,
    [month]           TINYINT    NOT NULL,
    month_name        VARCHAR(9) NOT NULL,
    day_of_month      TINYINT    NOT NULL,
    day_name          VARCHAR(9) NOT NULL,
    is_weekend        BIT        NOT NULL,
    financial_year    SMALLINT   NOT NULL,
    is_public_holiday BIT        NOT NULL CONSTRAINT df_dim_date_hol DEFAULT (0)
);
;WITH d AS (
    SELECT CAST('2025-01-01' AS DATE) AS dt
    UNION ALL SELECT DATEADD(DAY, 1, dt) FROM d WHERE dt < '2027-12-31'
)
INSERT INTO dbo.dim_date (date_key, full_date, [year], [quarter], [month],
                          month_name, day_of_month, day_name, is_weekend,
                          financial_year, is_public_holiday)
SELECT
    (YEAR(dt)*10000 + MONTH(dt)*100 + DAY(dt)),
    dt, YEAR(dt), DATEPART(QUARTER, dt), MONTH(dt), DATENAME(MONTH, dt),
    DAY(dt), DATENAME(WEEKDAY, dt),
    CASE WHEN DATENAME(WEEKDAY, dt) IN ('Saturday','Sunday') THEN 1 ELSE 0 END,
    CASE WHEN MONTH(dt) >= 7 THEN YEAR(dt) + 1 ELSE YEAR(dt) END,
    0
FROM d
OPTION (MAXRECURSION 0);
