"""Unit tests for the C/I/D/F parser.

No Azure, no network — the parser is a pure function of the text it's given,
which is exactly what makes these tests fast and trustworthy. Sample data
below mirrors real bronze files (trimmed to the columns that matter).
"""

from __future__ import annotations

from datetime import timedelta

from nem.parser import (
    iter_records,
    parse_dispatch_price,
    parse_nem_timestamp,
    parse_unit_scada,
)

# A DISPATCHIS report with a table we ignore (CASE_SOLUTION) and the one we
# want (PRICE, two regions). Ends with a C comment, not an F row.
DISPATCHIS = """\
C,NEMP.WORLD,DISPATCHIS,AEMO,PUBLIC,2026/07/21,15:20:11,0000000528544015,DISPATCHIS,0000000528544014
I,DISPATCH,CASE_SOLUTION,2,SETTLEMENTDATE,RUNNO,INTERVENTION
D,DISPATCH,CASE_SOLUTION,2,"2026/07/21 15:25:00",1,0
I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,INTERVENTION,RRP,EEP,ROP,APCFLAG,MARKETSUSPENDEDFLAG,LASTCHANGED
D,DISPATCH,PRICE,5,"2026/07/21 15:25:00",1,NSW1,20260721137,0,85.37954,0,85.37954,0,0,"2026/07/21 15:20:06"
D,DISPATCH,PRICE,5,"2026/07/21 15:25:00",1,QLD1,20260721137,0,78.51,0,78.51,0,0,"2026/07/21 15:20:06"
C,"END OF REPORT",6
"""

DISPATCH_SCADA = """\
C,NEMP.WORLD,DISPATCHSCADA,AEMO,PUBLIC,2026/07/21,15:10:11,0000000528542738,DISPATCHSCADA,0000000528542732
I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE,LASTCHANGED
D,DISPATCH,UNIT_SCADA,1,"2026/07/21 15:15:00",BARCSF1,12.60,"2026/07/21 15:10:10"
D,DISPATCH,UNIT_SCADA,1,"2026/07/21 15:15:00",BATTERY1,-5.00,"2026/07/21 15:10:10"
C,"END OF REPORT",4
"""


def _replace_rrp(text: str, region: str, new_rrp: str) -> str:
    """Swap the RRP value for one region's PRICE row, for the reject tests."""
    out = []
    for line in text.splitlines():
        parts = line.split(",")
        if line.startswith("D,DISPATCH,PRICE") and parts[6] == region:
            parts[9] = new_rrp
            line = ",".join(parts)
        out.append(line)
    return "\n".join(out)


# --- happy paths ------------------------------------------------------------

def test_parses_price_happy_path():
    valid, rejects = parse_dispatch_price(DISPATCHIS)
    assert rejects == []
    assert len(valid) == 2
    nsw = next(r for r in valid if r.regionid == "NSW1")
    assert nsw.rrp == 85.37954


def test_scada_happy_path_allows_negative():
    valid, rejects = parse_unit_scada(DISPATCH_SCADA)
    assert rejects == []
    assert len(valid) == 2
    battery = next(r for r in valid if r.duid == "BATTERY1")
    assert battery.scadavalue == -5.0  # charging — negative output is valid


# --- the AEST rule ----------------------------------------------------------

def test_price_timestamp_is_aest():
    valid, _ = parse_dispatch_price(DISPATCHIS)
    ts = valid[0].settlementdate
    assert ts.utcoffset() == timedelta(hours=10)
    assert (ts.hour, ts.minute) == (15, 25)  # SETTLEMENTDATE, not filename time


def test_aest_has_no_daylight_saving():
    # A summer date must still be +10, never +11. This is the rule that keeps
    # every summer figure from silently shifting an hour.
    summer = parse_nem_timestamp("2026/01/15 14:00:00")
    assert summer.utcoffset() == timedelta(hours=10)


# --- selecting the right table ----------------------------------------------

def test_ignores_other_tables():
    # CASE_SOLUTION has a D row too, but only the 2 PRICE rows come back.
    valid, _ = parse_dispatch_price(DISPATCHIS)
    assert {r.regionid for r in valid} == {"NSW1", "QLD1"}


def test_reads_columns_by_name_via_i_row():
    rows = list(iter_records(DISPATCHIS, {"DISPATCH.PRICE"}))
    _key, first = rows[0]
    assert first["REGIONID"] == "NSW1"
    assert first["RRP"] == "85.37954"


# --- validation / reject path -----------------------------------------------

def test_out_of_bounds_price_is_rejected():
    bad = _replace_rrp(DISPATCHIS, "NSW1", "999999")
    valid, rejects = parse_dispatch_price(bad)
    assert len(valid) == 1  # QLD survives
    assert len(rejects) == 1
    assert "rrp" in rejects[0].reason.lower()


def test_malformed_rrp_is_rejected():
    bad = _replace_rrp(DISPATCHIS, "NSW1", "not_a_number")
    _valid, rejects = parse_dispatch_price(bad)
    assert len(rejects) == 1


def test_reject_carries_reason_and_raw_row():
    bad = _replace_rrp(DISPATCHIS, "NSW1", "999999")
    _valid, rejects = parse_dispatch_price(bad)
    r = rejects[0]
    assert r.table == "DISPATCH.PRICE"
    assert r.reason  # non-empty
    assert r.raw["REGIONID"] == "NSW1"  # the offending row is preserved


# --- edge cases -------------------------------------------------------------

def test_empty_text_yields_nothing():
    assert parse_dispatch_price("") == ([], [])


def test_csv_handles_quoted_fields():
    # Timestamps are quoted; the csv module must treat the space, not commas,
    # correctly. A naive split(",") would mangle a quoted field with a comma.
    quoted = (
        'I,X,T,1,SETTLEMENTDATE,NOTE\n'
        'D,X,T,1,"2026/07/21 15:25:00","a, b, c"\n'
    )
    (_key, row), = list(iter_records(quoted, {"X.T"}))
    assert row["NOTE"] == "a, b, c"  # comma preserved inside the quoted field
