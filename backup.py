"""Create a consistent SQLite backup without copying live WAL files."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import DB_PATH


def main() -> None:
    source_path = Path(DB_PATH)
    if not source_path.exists():
        raise SystemExit("LeadScout database does not exist yet")
    destination_dir = Path(os.getenv("BACKUP_DIR", str(source_path.parent / "backups")))
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d_%H-%M-%S")
    destination = destination_dir / f"leadscout_{stamp}.db"
    # mode=ro protects the source database. The directory must still be writable
    # so SQLite can create WAL/SHM sidecars when no other connection holds them.
    source_uri = f"file:{source_path.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    with sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True) as restored:
        integrity = restored.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            destination.unlink(missing_ok=True)
            raise SystemExit("Backup integrity check failed")
    (destination_dir / ".last_success").write_text(destination.name, encoding="utf-8")
    backups = sorted(destination_dir.glob("leadscout_*.db"), reverse=True)
    for old_backup in backups[14:]:
        old_backup.unlink()


if __name__ == "__main__":
    if "--check" in sys.argv:
        directory = Path(os.getenv("BACKUP_DIR", str(Path(DB_PATH).parent / "backups")))
        marker = directory / ".last_success"
        max_age = int(os.getenv("BACKUP_MAX_AGE_HOURS", "30")) * 3600
        if not marker.exists() or datetime.now().timestamp() - marker.stat().st_mtime > max_age:
            raise SystemExit("No recent successful backup")
        backup = directory / marker.read_text(encoding="utf-8").strip()
        if not backup.exists():
            raise SystemExit("Recorded backup is missing")
        with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as restored:
            if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("Backup is corrupt")
    else:
        main()
