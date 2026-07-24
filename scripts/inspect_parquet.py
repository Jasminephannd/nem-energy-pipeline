"""Inspect a Parquet file: schema, physical type annotations, and sample rows.

Parquet is binary, so you can't just open it in a text editor. This prints
the three things you actually need when debugging a downstream reader:

  1. Arrow schema      - the logical types (timestamp[us], string, double...)
  2. Parquet schema    - the PHYSICAL types + annotations. This is the one
                         that matters for interop: a reader that doesn't
                         understand the annotation falls back to the raw
                         physical type (int64 -> "long"), which is exactly
                         how timestamps get mangled in ADF.
  3. Sample rows       - so you can eyeball actual values.

Usage (venv active):
    # a local file you downloaded
    python scripts/inspect_parquet.py path\\to\\file.parquet

    # straight from the storage account (needs NEM_STORAGE_ACCOUNT + az login)
    python scripts/inspect_parquet.py --container silver --prefix dispatch_price/
    python scripts/inspect_parquet.py --container silver --blob dispatch_price/2026/07/21/X.parquet

    # check schema consistency across MANY files (the "are they all the same?" question)
    python scripts/inspect_parquet.py --container silver --prefix dispatch_price/ --scan-all
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from collections import Counter

import pyarrow.parquet as pq


def _container_client(account: str, container: str):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient(
        f"https://{account}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )
    return service.get_container_client(container)


def describe(raw: bytes, label: str, rows: int) -> None:
    pf = pq.ParquetFile(io.BytesIO(raw))

    print(f"\n=== {label} ===")
    print(f"rows: {pf.metadata.num_rows}   row groups: {pf.metadata.num_row_groups}")

    print("\n--- Arrow schema (logical types) ---")
    print(pf.schema_arrow)

    print("\n--- Parquet schema (physical types + annotations) ---")
    # The annotation in brackets is what a downstream reader keys off.
    for i in range(len(pf.schema)):
        print(" ", pf.schema.column(i))

    print(f"\n--- first {rows} rows ---")
    df = pf.read().to_pandas().head(rows)
    with_pd_opts = df.to_string(index=False)
    print(with_pd_opts)

    print("\n--- pandas dtypes ---")
    print(df.dtypes.to_string())


def scan_all(container, prefix: str) -> None:
    """Report how many distinct schemas exist across every .parquet under prefix."""
    names = [b.name for b in container.list_blobs(name_starts_with=prefix)
             if b.name.lower().endswith(".parquet")]
    print(f"scanning {len(names)} files under {prefix!r} ...")
    schemas: Counter[str] = Counter()
    for n in names:
        raw = container.get_blob_client(n).download_blob().readall()
        schemas[str(pq.ParquetFile(io.BytesIO(raw)).schema_arrow)] += 1

    print(f"\ndistinct schemas found: {len(schemas)}")
    for schema, count in schemas.most_common():
        print(f"\n--- {count} file(s) ---")
        print(schema)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inspect Parquet schema and sample rows.")
    p.add_argument("path", nargs="?", help="Local .parquet file to inspect.")
    p.add_argument("--account", default=os.environ.get("NEM_STORAGE_ACCOUNT"))
    p.add_argument("--container", help="Blob container (e.g. silver).")
    p.add_argument("--blob", help="Exact blob name to inspect.")
    p.add_argument("--prefix", help="Inspect the first .parquet under this prefix.")
    p.add_argument("--rows", type=int, default=5, help="Sample rows to print.")
    p.add_argument("--scan-all", action="store_true",
                   help="With --prefix: report distinct schemas across ALL files.")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if args.path:
        with open(args.path, "rb") as fh:
            describe(fh.read(), args.path, args.rows)
        return 0

    if not args.container:
        print("ERROR: give a local path, or --container with --blob/--prefix.",
              file=sys.stderr)
        return 2
    if not args.account:
        print("ERROR: set --account or NEM_STORAGE_ACCOUNT.", file=sys.stderr)
        return 2

    container = _container_client(args.account, args.container)

    if args.scan_all:
        if not args.prefix:
            print("ERROR: --scan-all needs --prefix.", file=sys.stderr)
            return 2
        scan_all(container, args.prefix)
        return 0

    name = args.blob
    if not name:
        name = next((b.name for b in container.list_blobs(name_starts_with=args.prefix or "")
                     if b.name.lower().endswith(".parquet")), None)
    if not name:
        print("No .parquet found.", file=sys.stderr)
        return 1

    raw = container.get_blob_client(name).download_blob().readall()
    describe(raw, name, args.rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
