/* ============================================================
   NEM gold star schema - FACTS
   Apply AFTER 01_dimensions.sql.
   ============================================================ */

/* ---- fct_dispatch_price ------------------------------------
   Grain: one row per region per 5-minute dispatch interval,
          per intervention flag. Measure: rrp ($/MWh).
   settlement_date is the interval-ending time in AEST.
   date_key / interval_key are DERIVED from settlement_date at
   load time (the ADF transform computes them), then join back
   to dim_date / dim_interval.                                  */
IF OBJECT_ID('dbo.fct_dispatch_price','U') IS NOT NULL DROP TABLE dbo.fct_dispatch_price;
CREATE TABLE dbo.fct_dispatch_price (
    settlement_date DATETIME2(0)  NOT NULL,
    region_id       VARCHAR(10)   NOT NULL,
    intervention    TINYINT       NOT NULL,
    rrp             DECIMAL(12,5)  NOT NULL,   -- $/MWh
    date_key        INT           NOT NULL,
    interval_key    SMALLINT      NOT NULL,
    CONSTRAINT pk_fct_dispatch_price
        PRIMARY KEY (settlement_date, region_id, intervention),
    CONSTRAINT fk_price_region
        FOREIGN KEY (region_id)    REFERENCES dbo.dim_region(region_id),
    CONSTRAINT fk_price_date
        FOREIGN KEY (date_key)     REFERENCES dbo.dim_date(date_key),
    CONSTRAINT fk_price_interval
        FOREIGN KEY (interval_key) REFERENCES dbo.dim_interval(interval_key)
);

/* ---- fct_unit_dispatch -------------------------------------
   Grain: one row per generating unit (DUID) per 5-minute
          dispatch interval. Measure: scada_mw (MW, may be
          negative - batteries charging, pumped hydro pumping).

   NOTE: no FK to dim_unit yet. The DUID -> fuel type /
   renewable flag dimension needs AEMO's registration list,
   which isn't wired in. duid is stored as the natural key so
   the FK can be added later without reshaping the fact.        */
IF OBJECT_ID('dbo.fct_unit_dispatch','U') IS NOT NULL DROP TABLE dbo.fct_unit_dispatch;
CREATE TABLE dbo.fct_unit_dispatch (
    settlement_date DATETIME2(0)  NOT NULL,
    duid            VARCHAR(20)   NOT NULL,
    scada_mw        DECIMAL(12,4) NOT NULL,
    date_key        INT           NOT NULL,
    interval_key    SMALLINT      NOT NULL,
    CONSTRAINT pk_fct_unit_dispatch PRIMARY KEY (settlement_date, duid),
    CONSTRAINT fk_scada_date
        FOREIGN KEY (date_key)     REFERENCES dbo.dim_date(date_key),
    CONSTRAINT fk_scada_interval
        FOREIGN KEY (interval_key) REFERENCES dbo.dim_interval(interval_key)
);
