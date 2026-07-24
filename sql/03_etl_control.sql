/* ============================================================
   ETL control table - the metadata that drives the ADF pipeline.

   The pipeline does NOT know about any particular dataset. It
   reads this table, loops the active rows, and runs the same
   parameterised Data Flow for each. Onboarding a new AEMO table
   is an INSERT here plus a silver dataset - no pipeline change.

   This is the "metadata-driven, parameterised pipeline" pattern
   the role description asks for.
   ============================================================ */

IF OBJECT_ID('dbo.etl_control','U') IS NOT NULL DROP TABLE dbo.etl_control;
CREATE TABLE dbo.etl_control (
    dataset_name VARCHAR(50)  NOT NULL CONSTRAINT pk_etl_control PRIMARY KEY,
    source_path  VARCHAR(200) NOT NULL,  -- wildcard, relative to the silver container
    target_table VARCHAR(100) NOT NULL,  -- table in the gold schema
    is_active    BIT          NOT NULL CONSTRAINT df_etl_active DEFAULT (1),
    notes        VARCHAR(200) NULL
);

INSERT INTO dbo.etl_control (dataset_name, source_path, target_table, is_active, notes) VALUES
    ('dispatch_price', 'dispatch_price/**/*.parquet', 'fct_dispatch_price', 1,
     'Regional spot price (RRP) per 5-min interval, from DISPATCHIS.'),
    ('unit_scada',     'unit_scada/**/*.parquet',     'fct_unit_dispatch',  1,
     'Per-DUID metered output per 5-min interval, from DISPATCH_SCADA.');

/* The pipeline's Lookup activity runs this: */
-- SELECT dataset_name, source_path, target_table FROM dbo.etl_control WHERE is_active = 1;
