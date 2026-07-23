"""Peek inside a bronze zip to see AEMO's C/I/D/F structure.

Exploration tool — downloads one report zip from the bronze container,
unzips it in memory, and prints its shape so you can eyeball the format
before writing the parser:

  * every C row (comments: file metadata header/footer)
  * every I row (defines the columns for the D rows that follow)
  * the first couple of D rows after each I (sample data)

This reveals the key fact that shapes the parser: a single file contains
multiple I/D blocks for different tables.

Run (venv active, NEM_STORAGE_ACCOUNT set):
    python scripts/peek_bronze.py
    python scripts/peek_bronze.py --feed dispatch_scada --sample-rows 3
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import zipfile

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

BRONZE_CONTAINER = "bronze"


def first_blob_name(container, prefix: str) -> str | None:
    """Return the name of the first .zip blob found under a feed prefix.

    ADLS Gen2 (hierarchical namespace) surfaces real directory objects in
    list_blobs, so we must skip anything that isn't an actual .zip file.
    """
    for blob in container.list_blobs(name_starts_with=prefix):
        if blob.name.lower().endswith(".zip"):
            return blob.name
    return None


def csv_text_from_zip(raw: bytes) -> str:
    """Extract the single CSV member from an AEMO report zip."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ValueError(f"No .csv inside zip; members were {zf.namelist()}")
        return zf.read(members[0]).decode("utf-8", errors="replace")


def describe(csv_text: str, sample_rows: int) -> None:
    """Print C and I rows in full, plus the first few D rows after each I."""
    d_since_i = 0
    for line in csv_text.splitlines():
        if not line:
            continue
        row_type = line[0]

        if row_type in ("C", "I", "F"):
            d_since_i = 0
            label = {"C": "COMMENT", "I": "TABLE  ", "F": "FOOTER "}[row_type]
            # For I rows, show the table name (fields 2 and 3) prominently.
            if row_type == "I":
                fields = line.split(",")
                table = ".".join(fields[1:3]) if len(fields) >= 3 else "?"
                print(f"\n[{label}] === {table} ===")
            print(f"[{label}] {line[:160]}")
        elif row_type == "D":
            if d_since_i < sample_rows:
                print(f"[DATA   ] {line[:160]}")
                d_since_i += 1
            elif d_since_i == sample_rows:
                print("[DATA   ] ... (more rows) ...")
                d_since_i += 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inspect a bronze report zip.")
    p.add_argument("--account", default=os.environ.get("NEM_STORAGE_ACCOUNT"))
    p.add_argument("--feed", default="dispatchis",
                   help="Bronze prefix to sample (default: dispatchis).")
    p.add_argument("--sample-rows", type=int, default=2,
                   help="D rows to show after each table (default: 2).")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.account:
        print("ERROR: set --account or NEM_STORAGE_ACCOUNT.", file=sys.stderr)
        return 2

    account_url = f"https://{args.account}.blob.core.windows.net"
    service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    container = service.get_container_client(BRONZE_CONTAINER)

    name = first_blob_name(container, f"{args.feed}/")
    if not name:
        print(f"No blobs under prefix {args.feed}/", file=sys.stderr)
        return 1

    print(f"Inspecting: {name}\n" + "=" * 70)
    raw = container.get_blob_client(name).download_blob().readall()
    describe(csv_text_from_zip(raw), args.sample_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
