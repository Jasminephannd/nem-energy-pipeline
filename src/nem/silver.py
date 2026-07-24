"""Silver layer: bronze zips -> parsed, validated Parquet (+ rejects).

For each immutable bronze zip: download it, unzip in memory, run the pure
parser, then write the validated records to silver/ as Parquet and any
rejects to rejected/ with their failure reason. Idempotent — a silver file
that already exists is skipped, so re-runs are safe and cheap.

Layering (same principle as the extractor): the messy I/O lives here; the
parsing/validation logic is the pure `nem.parser` module this calls into.

Run (venv active, NEM_STORAGE_ACCOUNT set):
    python -m nem.silver --dry-run
    python -m nem.silver
    python -m nem.silver --dataset dispatch_price
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from nem.parser import Reject, parse_dispatch_price, parse_unit_scada

BRONZE_CONTAINER = "bronze"
SILVER_CONTAINER = "silver"
REJECTED_CONTAINER = "rejected"


@dataclass(frozen=True)
class Dataset:
    """Maps one silver dataset to its bronze source, parser and output shape."""

    name: str  # silver folder, e.g. "dispatch_price"
    bronze_prefix: str  # bronze feed prefix, e.g. "dispatchis"
    parser: Callable[[str], tuple[list, list[Reject]]]
    # Parser field -> gold column name. Doubles as a column selector: fields
    # not listed here are dropped. Conforming names to the gold model is a
    # silver responsibility - it's what lets ONE generic, parameterised ADF
    # Data Flow serve every dataset instead of one bespoke flow per table.
    column_map: dict[str, str]


# Metadata-driven, like the extractor's FEEDS: adding a dataset is one entry.
DATASETS: dict[str, Dataset] = {
    "dispatch_price": Dataset(
        "dispatch_price", "dispatchis", parse_dispatch_price,
        {
            "settlementdate": "settlement_date",
            "regionid": "region_id",
            "intervention": "intervention",
            "rrp": "rrp",
        },
    ),
    "unit_scada": Dataset(
        "unit_scada", "dispatch_scada", parse_unit_scada,
        {
            "settlementdate": "settlement_date",
            "duid": "duid",
            "scadavalue": "scada_mw",
        },
    ),
}


# --- pure helpers ------------------------------------------------------------

def csv_text_from_zip(raw: bytes) -> str:
    """Extract the single CSV member from an AEMO report zip."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ValueError(f"No .csv inside zip; members were {zf.namelist()}")
        return zf.read(members[0]).decode("utf-8", errors="replace")


def silver_path(dataset: Dataset, bronze_name: str) -> str:
    """dispatchis/2026/07/21/PUBLIC_x.zip -> dispatch_price/2026/07/21/PUBLIC_x.parquet"""
    tail = bronze_name.split("/", 1)[1]  # drop the feed prefix
    stem = tail.rsplit(".", 1)[0]
    return f"{dataset.name}/{stem}.parquet"


def rejected_path(dataset: Dataset, bronze_name: str) -> str:
    tail = bronze_name.split("/", 1)[1]
    stem = tail.rsplit(".", 1)[0]
    return f"{dataset.name}/{stem}.rejects.parquet"


TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def timestamps_to_iso_strings(df: pd.DataFrame) -> pd.DataFrame:
    """Write timestamps as ISO strings rather than binary Parquet timestamps.

    Why not a real timestamp type? ADF Mapping Data Flow's Parquet reader
    only recognises the *legacy* ConvertedType annotation, and that
    annotation is defined as UTC-normalised. NEM time is naive AEST
    (isAdjustedToUTC=false), which has no legacy representation - so
    pyarrow correctly writes `converted_type: NONE`, ADF sees an
    unannotated INT64, and the column reads as null.

    Storing 'YYYY-MM-DD HH:MM:SS' strings sidesteps the whole interop
    problem: every reader agrees on strings, and the value is legible to a
    human opening the file. The Data Flow casts back with toTimestamp().

    The AEST rule is not weakened - it lives in the parser and its tests,
    which assert the +10:00 offset. These strings are AEST wall-clock.
    """
    for col in df.columns:
        dtype = df[col].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            df[col] = df[col].dt.tz_localize(None)
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime(TIMESTAMP_FORMAT)
    return df


def records_to_parquet(records: list, column_map: dict[str, str]) -> bytes:
    df = timestamps_to_iso_strings(pd.DataFrame([r.model_dump() for r in records]))
    # Select + rename to the gold column names in one step.
    df = df[list(column_map)].rename(columns=column_map)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def rejects_to_parquet(rejects: list[Reject]) -> bytes:
    df = pd.DataFrame([{**r.raw, "_reject_reason": r.reason} for r in rejects])
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


# --- orchestration -----------------------------------------------------------

def run(account: str, datasets: list[Dataset], overwrite: bool, dry_run: bool) -> None:
    service = BlobServiceClient(
        f"https://{account}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )
    bronze = service.get_container_client(BRONZE_CONTAINER)
    silver = service.get_container_client(SILVER_CONTAINER)
    rejected = service.get_container_client(REJECTED_CONTAINER)

    for ds in datasets:
        processed = skipped = rows = rej_rows = 0

        for blob in bronze.list_blobs(name_starts_with=f"{ds.bronze_prefix}/"):
            if not blob.name.lower().endswith(".zip"):  # skip Gen2 directories
                continue

            spath = silver_path(ds, blob.name)
            sblob = silver.get_blob_client(spath)
            if not overwrite and sblob.exists():
                skipped += 1
                continue

            if dry_run:
                print(f"  would transform {blob.name} -> {spath}")
                processed += 1
                continue

            raw = bronze.get_blob_client(blob.name).download_blob().readall()
            valid, rejects = ds.parser(csv_text_from_zip(raw))

            if valid:
                sblob.upload_blob(records_to_parquet(valid, ds.column_map), overwrite=True)
                rows += len(valid)
            if rejects:
                rej_blob = rejected.get_blob_client(rejected_path(ds, blob.name))
                rej_blob.upload_blob(rejects_to_parquet(rejects), overwrite=True)
                rej_rows += len(rejects)
            processed += 1

        verb = "would process" if dry_run else "processed"
        print(f"[{ds.name}] {verb} {processed}, skipped {skipped}, "
              f"rows {rows}, rejects {rej_rows}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Transform bronze zips into silver Parquet.")
    p.add_argument("--account", default=os.environ.get("NEM_STORAGE_ACCOUNT"))
    p.add_argument("--dataset", nargs="*", choices=sorted(DATASETS),
                   default=sorted(DATASETS), help="Which datasets (default: all).")
    p.add_argument("--overwrite", action="store_true",
                   help="Re-transform even if the silver file already exists.")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be transformed without writing.")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.account:
        print("ERROR: set --account or NEM_STORAGE_ACCOUNT.", file=sys.stderr)
        return 2

    run(
        account=args.account,
        datasets=[DATASETS[name] for name in args.dataset],
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
