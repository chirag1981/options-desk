"""
scripts/backup_observation_db.py — Online Zero-Downtime Backup Utility for Observation Ledger

Uses SQLite's native online backup API (`sqlite3.Connection.backup`) to create atomic,
consistent snapshot backups without locking out the observation engine.
"""

import os
import sys
import sqlite3
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE_DB = os.path.join(BASE_DIR, "instance", "research_paper_trading", "observation_ledger.db")
BACKUP_DIR = os.path.join(BASE_DIR, "instance", "research_paper_trading", "backups")


def backup_observation_database(source_path: str = SOURCE_DB, backup_dir: str = BACKUP_DIR) -> str:
    """Performs an online, transactionally consistent backup of the SQLite ledger."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source database does not exist: {source_path}")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"observation_ledger_{timestamp}.db")

    src_conn = sqlite3.connect(source_path)
    dst_conn = sqlite3.connect(dest_path)
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        src_conn.close()
        dst_conn.close()

    return dest_path


def restore_observation_database(backup_file: str, target_path: str = SOURCE_DB) -> None:
    """Restores database from a designated backup snapshot."""
    if not os.path.exists(backup_file):
        raise FileNotFoundError(f"Backup file not found: {backup_file}")

    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    src_conn = sqlite3.connect(backup_file)
    dst_conn = sqlite3.connect(target_path)
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=100)
    finally:
        src_conn.close()
        dst_conn.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restore":
        if len(sys.argv) < 3:
            print("Usage: python scripts/backup_observation_db.py --restore <path_to_backup>")
            sys.exit(1)
        restore_observation_database(sys.argv[2])
        print(f"Restored observation database from: {sys.argv[2]}")
    else:
        out = backup_observation_database()
        print(f"Observation database backed up successfully to: {out}")
