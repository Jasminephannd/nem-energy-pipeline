"""NEMWeb extractor — lands raw AEMO report zips into the bronze container.

Bronze is the immutable audit layer: zips arrive byte-for-byte as AEMO
published them. No unzipping, no parsing, no transformation happens here.
That is the whole point — if a downstream parse is ever disputed, the
original source is still sitting in bronze untouched.

Design notes worth defending in an interview:
  * Metadata-driven. Feeds live in the FEEDS dict, not in the code paths.
    Adding a third AEMO report is one dict entry, mirroring the ADF
    control-table pattern used later in the build.
  * Idempotent. A blob already present in bronze is skipped, so the
    extractor can be re-run freely without creating duplicates.
  * Keyless auth. DefaultAzureCredential reuses the `az login` session;
    the deploying user holds Storage Blob Data Contributor via the Bicep
    role assignment. No connection strings, no keys in code or config.
  * Partitioned by business time. The path date comes from the interval
    timestamp inside the filename, not the wall-clock download time, so
    re-running tomorrow still files today's data under today.

Run:
    python -m nem.extractor --limit 12 --dry-run
    python -m nem.extractor --limit 12
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NEMWEB_BASE = "https://nemweb.com.au/Reports/Current"

# A request without a browser-like User-Agent is sometimes refused by nemweb.
_HTTP_HEADERS = {"User-Agent": "nem-energy-pipeline/1.0 (portfolio project)"}

# nemweb throttles aggressive callers with HTTP 403/429. Two defences:
#   1. a small pause between downloads so we stay under the rate limit, and
#   2. retry-with-backoff when we hit it anyway.
REQUEST_DELAY_SECONDS = 0.25


def _build_session() -> requests.Session:
    """Session that retries throttling responses with exponential backoff."""
    session = requests.Session()
    session.headers.update(_HTTP_HEADERS)
    retry = Retry(
        total=5,
        backoff_factor=1.5,  # exponential: ~1.5s, 3s, 6s, 12s, 24s between tries
        status_forcelist=(403, 429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


_SESSION = _build_session()


@dataclass(frozen=True)
class Feed:
    """One AEMO report feed we mirror into bronze."""

    name: str  # bronze partition prefix, e.g. "dispatchis"
    directory: str  # nemweb Current/ subdirectory
    filename_prefix: str  # used to pull the timestamp out of the filename


# The two feeds in scope. DISPATCHPRICE is bundled inside the DISPATCHIS
# report; DISPATCH_UNIT_SCADA is its own report.
FEEDS: dict[str, Feed] = {
    "dispatchis": Feed(
        name="dispatchis",
        directory="DispatchIS_Reports",
        filename_prefix="PUBLIC_DISPATCHIS_",
    ),
    "dispatch_scada": Feed(
        name="dispatch_scada",
        directory="Dispatch_SCADA",
        filename_prefix="PUBLIC_DISPATCHSCADA_",
    ),
}

BRONZE_CONTAINER = "bronze"


# --- Pure helpers (no network, no Azure — unit-testable) --------------------

def parse_interval_timestamp(filename: str, prefix: str) -> datetime:
    """Extract the interval timestamp (YYYYMMDDHHMM) from a NEMWeb filename.

    Returned datetime is naive and represents AEST (UTC+10). NEM time never
    observes daylight saving — see the parser module for the full rule.
    """
    match = re.search(rf"{re.escape(prefix)}(\d{{12}})", filename)
    if not match:
        raise ValueError(f"No YYYYMMDDHHMM timestamp in filename: {filename!r}")
    return datetime.strptime(match.group(1), "%Y%m%d%H%M")


def blob_path(feed_name: str, filename: str, ts: datetime) -> str:
    """Build the bronze blob path, partitioned by the interval's date."""
    return f"{feed_name}/{ts:%Y/%m/%d}/{filename}"


# --- Network -----------------------------------------------------------------

def list_available_files(feed: Feed) -> list[str]:
    """Return the .zip filenames listed in a feed's Current/ directory."""
    url = f"{NEMWEB_BASE}/{feed.directory}/"
    resp = _SESSION.get(url, timeout=30)
    resp.raise_for_status()
    # The listing is an HTML index; pull every href ending in .zip.
    names = re.findall(r'href="[^"]*?([^"/]+\.zip)"', resp.text, re.IGNORECASE)
    # De-duplicate while preserving order, then sort chronologically (the
    # timestamp sorts lexically because it is fixed-width YYYYMMDDHHMM).
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(n, None)
    return sorted(seen)


def download_zip(feed: Feed, filename: str) -> bytes:
    """Download one report zip. Files are small (tens of KB), so in-memory."""
    url = f"{NEMWEB_BASE}/{feed.directory}/{filename}"
    resp = _SESSION.get(url, timeout=60)
    resp.raise_for_status()
    return resp.content


# --- Azure -------------------------------------------------------------------

def _container_client(account: str):
    """Build a bronze ContainerClient using the az login identity."""
    # Imported here so the pure helpers can be unit-tested without azure libs.
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    account_url = f"https://{account}.blob.core.windows.net"
    service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    return service.get_container_client(BRONZE_CONTAINER)


# --- Orchestration -----------------------------------------------------------

def run(account: str, feeds: list[Feed], limit: int, dry_run: bool) -> None:
    container = None if dry_run else _container_client(account)
    uploaded = skipped = failed = 0

    for feed in feeds:
        available = list_available_files(feed)
        # Take the most recent `limit` intervals (list is sorted ascending).
        selected = available[-limit:] if limit else available
        print(f"\n[{feed.name}] {len(available)} available, "
              f"processing {len(selected)}")

        for filename in selected:
            ts = parse_interval_timestamp(filename, feed.filename_prefix)
            path = blob_path(feed.name, filename, ts)

            if dry_run:
                print(f"  would upload -> {path}")
                continue

            blob = container.get_blob_client(path)
            if blob.exists():  # idempotency: never re-upload
                skipped += 1
                continue

            # One bad file (e.g. throttled past all retries, or rolled off the
            # Current window mid-run) must not abort the whole batch. Log and
            # move on — a re-run picks it up thanks to the exists() check above.
            try:
                data = download_zip(feed, filename)
            except requests.HTTPError as exc:
                failed += 1
                print(f"  FAILED {filename}: {exc}")
                continue

            blob.upload_blob(data, overwrite=False)
            uploaded += 1
            print(f"  uploaded -> {path}")
            time.sleep(REQUEST_DELAY_SECONDS)  # be polite to nemweb

    if dry_run:
        print("\nDry run — nothing uploaded.")
    else:
        print(f"\nDone. Uploaded {uploaded}, skipped {skipped} "
              f"(already in bronze), failed {failed}.")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Land NEMWeb report zips into bronze.")
    p.add_argument("--account", default=os.environ.get("NEM_STORAGE_ACCOUNT"),
                   help="Storage account name (or set NEM_STORAGE_ACCOUNT).")
    p.add_argument("--feeds", nargs="*", choices=sorted(FEEDS), default=sorted(FEEDS),
                   help="Which feeds to pull (default: all).")
    p.add_argument("--limit", type=int, default=12,
                   help="Most-recent N intervals per feed. 0 = all available.")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be uploaded without touching Azure.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if not args.dry_run and not args.account:
        print("ERROR: set --account or NEM_STORAGE_ACCOUNT.", file=sys.stderr)
        return 2
    run(
        account=args.account,
        feeds=[FEEDS[name] for name in args.feeds],
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
