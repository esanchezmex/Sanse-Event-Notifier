#!/usr/bin/env python3
"""Cleanup raw ingest artifacts by retention policy.

Default policy:
- Delete .html files older than 30 days
- Delete .json files older than 90 days
- Remove empty directories left behind

Examples:
    python scraper/cleanup_raw.py
    python scraper/cleanup_raw.py --dry-run
    python scraper/cleanup_raw.py --raw-root data/raw --html-retention-days 30 --json-retention-days 90
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class CleanupStats:
    html_deleted: int = 0
    json_deleted: int = 0
    bytes_deleted: int = 0
    dirs_deleted: int = 0
    files_scanned: int = 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def should_delete(path: Path, cutoff: datetime) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return mtime < cutoff
    except FileNotFoundError:
        return False


def delete_if_old(path: Path, cutoff: datetime, dry_run: bool) -> tuple[bool, int]:
    if not should_delete(path, cutoff):
        return False, 0
    size = 0
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False, 0
    if dry_run:
        logging.info("[dry-run] Would delete %s", path)
        return True, size
    try:
        path.unlink()
        logging.info("Deleted %s", path)
        return True, size
    except FileNotFoundError:
        return False, 0


def cleanup_files(raw_root: Path, html_cutoff: datetime, json_cutoff: datetime, dry_run: bool) -> CleanupStats:
    stats = CleanupStats()
    if not raw_root.exists():
        logging.info("Raw root does not exist, nothing to clean: %s", raw_root)
        return stats

    for path in raw_root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        stats.files_scanned += 1
        if suffix == ".html":
            deleted, size = delete_if_old(path, html_cutoff, dry_run)
            if deleted:
                stats.html_deleted += 1
                stats.bytes_deleted += size
        elif suffix == ".json":
            deleted, size = delete_if_old(path, json_cutoff, dry_run)
            if deleted:
                stats.json_deleted += 1
                stats.bytes_deleted += size
    return stats


def remove_empty_dirs(root: Path, dry_run: bool) -> int:
    if not root.exists():
        return 0
    deleted = 0
    # Bottom-up walk so child dirs are removed first.
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        # Re-check directory content at this point.
        if any(path.iterdir()):
            continue
        if dry_run:
            logging.info("[dry-run] Would delete empty dir %s", path)
            deleted += 1
            continue
        try:
            path.rmdir()
            logging.info("Deleted empty dir %s", path)
            deleted += 1
        except OSError:
            # Directory may no longer be empty or may have vanished.
            continue
    return deleted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cleanup old raw HTML/JSON artifacts by retention policy")
    parser.add_argument("--raw-root", default="data/raw", help="Root directory for raw artifacts")
    parser.add_argument(
        "--html-retention-days",
        type=int,
        default=30,
        help="Delete .html files older than this many days",
    )
    parser.add_argument(
        "--json-retention-days",
        type=int,
        default=90,
        help="Delete .json files older than this many days",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without deleting files")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")

    raw_root = Path(args.raw_root)
    now = utc_now()
    html_cutoff = now - timedelta(days=args.html_retention_days)
    json_cutoff = now - timedelta(days=args.json_retention_days)

    logging.info(
        "Starting cleanup: raw_root=%s html_cutoff=%s json_cutoff=%s dry_run=%s",
        raw_root,
        html_cutoff.isoformat(),
        json_cutoff.isoformat(),
        args.dry_run,
    )

    stats = cleanup_files(raw_root=raw_root, html_cutoff=html_cutoff, json_cutoff=json_cutoff, dry_run=args.dry_run)
    stats.dirs_deleted = remove_empty_dirs(raw_root, dry_run=args.dry_run)

    logging.info(
        "Cleanup complete: files_scanned=%s html_deleted=%s json_deleted=%s dirs_deleted=%s bytes_deleted=%s",
        stats.files_scanned,
        stats.html_deleted,
        stats.json_deleted,
        stats.dirs_deleted,
        stats.bytes_deleted,
    )
    print(
        {
            "raw_root": str(raw_root),
            "html_retention_days": args.html_retention_days,
            "json_retention_days": args.json_retention_days,
            "dry_run": args.dry_run,
            "files_scanned": stats.files_scanned,
            "html_deleted": stats.html_deleted,
            "json_deleted": stats.json_deleted,
            "dirs_deleted": stats.dirs_deleted,
            "bytes_deleted": stats.bytes_deleted,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
