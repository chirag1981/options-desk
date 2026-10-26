"""
scripts/backup_db.py — Native Online Database Backup & Recovery for Primary Options Desk SQLite DB

Uses SQLite's online backup API (`sqlite3.Connection.backup`) to perform non-blocking,
transactionally consistent snapshots of `instance/options_desk.db`.

Features:
1. Online, point-in-time backup without stopping Flask or locking out background scheduler.
2. Backups stored in `instance/backups/`.
3. Configurable retention policy (default: retains 10 most recent backups). Never deletes active DB.
4. Backup validation: Verifies each backup can be opened and contains all required tables and schema.
5. Safe restore: Documented and tested restore procedure into a target database.
"""

import os
import sys
import glob
import sqlite3
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Optional

log = logging.getLogger("backup")
IST = ZoneInfo("Asia/Kolkata")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_SOURCE_DB = os.path.join(BASE_DIR, "instance", "options_desk.db")
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "instance", "backups")
DEFAULT_RETENTION_COUNT = 10

REQUIRED_TABLES = [
    "trade_positions",
    "position_legs",
    "signal_candidates",
    "risk_audit_logs",
    "oi_snapshots",
    "data_quality_events",
    "position_mtm_snapshots",
]


def backup_database(
    source_path: str = DEFAULT_SOURCE_DB,
    backup_dir: str = DEFAULT_BACKUP_DIR,
    retention_count: int = DEFAULT_RETENTION_COUNT,
) -> str:
    """
    Creates an online snapshot of the SQLite database.
    Prunes older backups to enforce retention policy while strictly preserving active DB.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source database does not exist: {source_path}")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"options_desk_{timestamp}.db")

    src_conn = sqlite3.connect(source_path, timeout=30.0)
    dst_conn = sqlite3.connect(dest_path, timeout=30.0)
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        src_conn.close()
        dst_conn.close()

    # Validate the generated backup immediately
    is_valid, validation_msg = verify_backup_integrity(dest_path)
    if not is_valid:
        os.remove(dest_path)
        raise RuntimeError(f"Backup verification failed: {validation_msg}")

    # Enforce retention policy
    if retention_count > 0:
        prune_old_backups(backup_dir, retention_count=retention_count)

    return dest_path


def verify_backup_integrity(backup_path: str) -> tuple[bool, str]:
    """
    Verifies that a backup file is a valid SQLite database containing required tables.
    """
    if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
        return False, "Backup file missing or empty"

    try:
        conn = sqlite3.connect(backup_path, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        res = cursor.fetchone()
        if not res or res[0] != "ok":
            conn.close()
            return False, f"Integrity check failed: {res}"

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        # Check for core tables
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            return False, f"Missing required tables in backup: {missing}"

        return True, "Backup integrity verified successfully"
    except Exception as e:
        return False, f"Error verifying backup: {str(e)}"


def prune_old_backups(backup_dir: str = DEFAULT_BACKUP_DIR, retention_count: int = DEFAULT_RETENTION_COUNT) -> List[str]:
    """
    Removes oldest backup files in excess of retention_count.
    Never removes non-backup files or files outside backup_dir.
    """
    pattern = os.path.join(backup_dir, "options_desk_*.db")
    backups = sorted(glob.glob(pattern))

    deleted = []
    if len(backups) > retention_count:
        to_delete = backups[: len(backups) - retention_count]
        for f in to_delete:
            try:
                os.remove(f)
                deleted.append(f)
            except Exception as e:
                log.warning(f"Failed pruning backup {f}: {e}")

    return deleted


def restore_database(backup_path: str, target_path: str) -> None:
    """
    Restores database from backup_path into target_path using online backup API.
    Refuses to execute if backup_path is invalid.
    """
    is_valid, msg = verify_backup_integrity(backup_path)
    if not is_valid:
        raise ValueError(f"Cannot restore: {msg}")

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
    src_conn = sqlite3.connect(backup_path, timeout=30.0)
    dst_conn = sqlite3.connect(target_path, timeout=30.0)
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        src_conn.close()
        dst_conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restore":
        if len(sys.argv) < 3:
            print("Usage: python scripts/backup_db.py --restore <path_to_backup> [target_path]")
            sys.exit(1)
        backup_src = sys.argv[2]
        target = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_SOURCE_DB
        restore_database(backup_src, target)
        print(f"Successfully restored database to: {target}")
    else:
        out = backup_database()
        print(f"Database backed up and verified successfully: {out}")
