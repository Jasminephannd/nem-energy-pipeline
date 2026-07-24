/* ============================================================
   Data-quality views — the DQ layer, as SQL views.

   Deliberately implemented as views (not a Great Expectations
   install) to stay lean: they run on demand, need no extra
   tooling, and double as the data source for the Power BI DQ
   page. The README notes GX / Microsoft Purview as how this
   would scale in a production/governed environment.

   AEMO dispatch data is ideal for DQ because the correct
   answers are EXACTLY knowable, not approximate:
     288 five-minute intervals/day x 5 regions = 1,440 price
     rows/day. A missing interval is a real, catchable defect.
   ============================================================ */

/* ---- vw_dq_price_completeness ------------------------------
   Actual vs expected price rows per day. Expected = 288 x 5.
   Any shortfall is a genuinely missing dispatch interval.     */
IF OBJECT_ID('dbo.vw_dq_price_completeness','V') IS NOT NULL
    DROP VIEW dbo.vw_dq_price_completeness;
GO
CREATE VIEW dbo.vw_dq_price_completeness AS
SELECT
    d.full_date,
    COUNT(*)                          AS actual_rows,
    288 * 5                           AS expected_rows,
    288 * 5 - COUNT(*)                AS missing_rows,
    CAST(100.0 * COUNT(*) / (288 * 5) AS DECIMAL(5,2)) AS completeness_pct
FROM dbo.fct_dispatch_price f
JOIN dbo.dim_date d ON d.date_key = f.date_key
WHERE f.intervention = 0          -- non-intervention run is the pricing case
GROUP BY d.full_date;
GO

/* ---- vw_dq_price_bounds ------------------------------------
   Prices outside the market floor/cap are parse errors, not
   market events. The parser already rejects hard-invalid rows;
   this surfaces anything that still looks implausible in gold. */
IF OBJECT_ID('dbo.vw_dq_price_bounds','V') IS NOT NULL
    DROP VIEW dbo.vw_dq_price_bounds;
GO
CREATE VIEW dbo.vw_dq_price_bounds AS
SELECT
    region_id,
    COUNT(*)                                              AS total_rows,
    SUM(CASE WHEN rrp < -1000 THEN 1 ELSE 0 END)          AS below_floor,
    SUM(CASE WHEN rrp > 20000 THEN 1 ELSE 0 END)          AS above_cap,
    SUM(CASE WHEN rrp < 0 THEN 1 ELSE 0 END)              AS negative_price_rows,
    MIN(rrp) AS min_rrp,
    MAX(rrp) AS max_rrp
FROM dbo.fct_dispatch_price
WHERE intervention = 0
GROUP BY region_id;
GO

/* ---- vw_dq_unit_coverage -----------------------------------
   How well dim_unit covers the DUIDs actually seen in SCADA,
   by count AND by generation volume. Unmatched = units in the
   feed but not in the current registration list (retired /
   renamed) - reported, not rejected (no FK by design).         */
IF OBJECT_ID('dbo.vw_dq_unit_coverage','V') IS NOT NULL
    DROP VIEW dbo.vw_dq_unit_coverage;
GO
CREATE VIEW dbo.vw_dq_unit_coverage AS
SELECT
    CASE WHEN u.duid IS NULL THEN 'unmatched' ELSE 'matched' END AS coverage,
    COUNT(DISTINCT f.duid) AS units,
    SUM(f.scada_mw)        AS total_mw
FROM dbo.fct_unit_dispatch f
LEFT JOIN dbo.dim_unit u ON u.duid = f.duid
GROUP BY CASE WHEN u.duid IS NULL THEN 'unmatched' ELSE 'matched' END;
GO

/* ---- vw_dq_scada_capacity ----------------------------------
   Physical plausibility: metered output vs registered capacity.

   INVESTIGATION FINDING (2 days, Jul 2026): 2,699 rows exceeded
   registered capacity +5%, but ALL fell in the 1.05-1.21x band
   and were concentrated in hydro (winter high water) and thermal
   (cool winter air/water lifts output) plants - i.e. legitimate
   over-nameplate operation, NOT data errors. Registered capacity
   is a nominal figure, not a hard ceiling. (An aggregation bug
   would show 5x-10x ratios; none seen.)

   So: this view is INFORMATIONAL (units running above nameplate,
   with the ratio), and the DQ summary counts only egregious
   cases (> 25x... > 1.25x) as genuine suspects.               */
IF OBJECT_ID('dbo.vw_dq_scada_capacity','V') IS NOT NULL
    DROP VIEW dbo.vw_dq_scada_capacity;
GO
CREATE VIEW dbo.vw_dq_scada_capacity AS
SELECT
    f.settlement_date, f.duid, u.station_name, u.fuel_category,
    f.scada_mw, u.capacity_mw,
    f.scada_mw - u.capacity_mw AS over_by_mw,
    CAST(f.scada_mw / u.capacity_mw AS DECIMAL(6,3)) AS ratio,
    CASE WHEN f.scada_mw > u.capacity_mw * 1.25 THEN 1 ELSE 0 END AS is_suspect
FROM dbo.fct_unit_dispatch f
JOIN dbo.dim_unit u ON u.duid = f.duid
WHERE u.capacity_mw IS NOT NULL
  AND f.scada_mw > u.capacity_mw * 1.05;   -- informational floor
GO

/* ---- vw_dq_summary -----------------------------------------
   One-row-per-check rollup for the Power BI DQ page tiles.     */
IF OBJECT_ID('dbo.vw_dq_summary','V') IS NOT NULL
    DROP VIEW dbo.vw_dq_summary;
GO
CREATE VIEW dbo.vw_dq_summary AS
SELECT 'Price completeness %'      AS check_name,
       CAST(AVG(completeness_pct) AS DECIMAL(6,2)) AS value
FROM dbo.vw_dq_price_completeness
UNION ALL
SELECT 'Price rows out of bounds',
       CAST(SUM(below_floor + above_cap) AS DECIMAL(6,2))
FROM dbo.vw_dq_price_bounds
UNION ALL
SELECT 'Unit coverage % by volume',
       CAST(100.0 * MAX(CASE WHEN coverage='matched' THEN total_mw END)
            / SUM(total_mw) AS DECIMAL(6,2))
FROM dbo.vw_dq_unit_coverage
UNION ALL
SELECT 'SCADA over-nameplate rows (informational)',
       CAST(COUNT(*) AS DECIMAL(6,2))
FROM dbo.vw_dq_scada_capacity
UNION ALL
SELECT 'SCADA suspect rows (> 1.25x capacity)',
       CAST(SUM(CAST(is_suspect AS INT)) AS DECIMAL(6,2))
FROM dbo.vw_dq_scada_capacity;
GO
