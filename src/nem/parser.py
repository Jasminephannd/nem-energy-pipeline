"""Parser for AEMO's MMS (C/I/D/F) report format -> validated records.

Pure module: no Azure, no network, no file I/O beyond the text handed in.
That is precisely what makes it unit-testable, which is the backbone of the
data-quality story this project is built around.

The format (confirmed by inspecting real bronze files):

    C,NEMP.WORLD,DISPATCHIS,AEMO,...          header comment
    I,DISPATCH,PRICE,5,SETTLEMENTDATE,...,RRP,...   column NAMES for the D rows
    D,DISPATCH,PRICE,5,"2026/07/21 15:25:00",...,85.37954,...   a data row
    ...
    C,"END OF REPORT",1005                     end marker + total row count

Key facts the parser depends on:
  * One file holds multiple I/D blocks for different tables. We keep only the
    tables asked for and ignore the rest.
  * The I row is the header for the D rows beneath it. We read D values BY
    COLUMN NAME, not by fixed position, so an AEMO version bump that inserts a
    column can't silently misalign the data. (The number after the table name,
    e.g. the `5` in DISPATCH,PRICE,5, is that version.)
  * Fields are proper CSV: timestamps are quoted and some text fields contain
    commas, so we parse with the csv module, never str.split(",").
  * There is no F footer row in these reports; the end marker is a C comment.
  * SETTLEMENTDATE inside the data is the authoritative interval key, in AEST.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ValidationError, field_validator

# NEM time is fixed AEST = UTC+10. No daylight saving, ever. Attaching this
# explicitly is what stops Windows local time silently shifting summer figures.
AEST = timezone(timedelta(hours=10))

# Generous sanity bounds to catch parse garbage (e.g. a mangled 999999 price),
# NOT to enforce the exact market cap. The precise cap/floor come from AEMO's
# MARKET_PRICE_THRESHOLDS and should be verified per financial year.
RRP_FLOOR = -1000.0
RRP_CAP = 20000.0


def parse_nem_timestamp(value: str) -> datetime:
    """Parse an AEMO 'YYYY/MM/DD HH:MM:SS' string as an AEST-aware datetime."""
    naive = datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")
    return naive.replace(tzinfo=AEST)


# --- The general C/I/D/F reader ---------------------------------------------

def iter_records(text: str, wanted: set[str]) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield (table_key, row) dicts for the D rows of the wanted tables.

    `table_key` looks like 'DISPATCH.PRICE'. Each row is a dict mapping the
    column names from the preceding I row to that D row's values.
    """
    headers: dict[str, list[str]] = {}
    for fields in csv.reader(io.StringIO(text)):
        if not fields:
            continue
        rowtype = fields[0]
        if rowtype == "I":
            # I,<group>,<table>,<version>,<col1>,<col2>,...  -> names from [4:]
            key = f"{fields[1]}.{fields[2]}"
            headers[key] = fields[4:]
        elif rowtype == "D":
            key = f"{fields[1]}.{fields[2]}"
            if key in wanted:
                cols = headers.get(key)
                if cols is None:
                    continue  # a D row before its I row — malformed file
                yield key, dict(zip(cols, fields[4:]))
        # C (comments/footer) and F rows carry no data we need here.


# --- Typed, validated contracts ---------------------------------------------

class DispatchPrice(BaseModel):
    """One region's spot price for one dispatch interval (DISPATCH.PRICE)."""

    settlementdate: datetime
    regionid: str
    rrp: float
    intervention: int
    lastchanged: datetime

    @field_validator("settlementdate", "lastchanged", mode="before")
    @classmethod
    def _parse_ts(cls, v: object) -> object:
        return parse_nem_timestamp(v) if isinstance(v, str) else v

    @field_validator("rrp")
    @classmethod
    def _check_bounds(cls, v: float) -> float:
        if not (RRP_FLOOR <= v <= RRP_CAP):
            raise ValueError(f"RRP {v} outside sanity bounds [{RRP_FLOOR}, {RRP_CAP}]")
        return v


class UnitScada(BaseModel):
    """One generating unit's metered output for one interval (DISPATCH.UNIT_SCADA)."""

    settlementdate: datetime
    duid: str
    scadavalue: float  # MW; may be negative (batteries charging, pumped hydro)
    lastchanged: datetime

    @field_validator("settlementdate", "lastchanged", mode="before")
    @classmethod
    def _parse_ts(cls, v: object) -> object:
        return parse_nem_timestamp(v) if isinstance(v, str) else v


# --- Extraction with a reject path ------------------------------------------

@dataclass
class Reject:
    """A D row that failed validation, kept with the reason for the DQ page."""

    table: str
    reason: str
    raw: dict[str, str]


def _extract(text: str, table_key: str, model: type[BaseModel]) -> tuple[list, list[Reject]]:
    """Validate every wanted D row; split into (valid records, rejects)."""
    valid: list = []
    rejects: list[Reject] = []
    fields = model.model_fields
    for _key, row in iter_records(text, {table_key}):
        # Map AEMO's UPPERCASE columns to the model's lowercase fields, keeping
        # only the fields the model declares (ignore the dozens of FCAS columns).
        data = {k.lower(): v for k, v in row.items() if k.lower() in fields}
        try:
            valid.append(model(**data))
        except ValidationError as exc:
            rejects.append(Reject(table_key, _first_error(exc), row))
    return valid, rejects


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"])
    return f"{loc}: {err['msg']}"


def parse_dispatch_price(text: str) -> tuple[list[DispatchPrice], list[Reject]]:
    """Extract validated DISPATCHPRICE records (and rejects) from one report."""
    return _extract(text, "DISPATCH.PRICE", DispatchPrice)


def parse_unit_scada(text: str) -> tuple[list[UnitScada], list[Reject]]:
    """Extract validated DISPATCH_UNIT_SCADA records (and rejects) from one report."""
    return _extract(text, "DISPATCH.UNIT_SCADA", UnitScada)
