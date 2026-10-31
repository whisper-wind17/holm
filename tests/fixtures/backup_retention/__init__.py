"""FX-M7-08B-01: opaque fixed-member backups; no Secrets or request bodies.

This fixture exercises the retention envelope, not payload restore validity.
M7-08A's real PG/SQLite/encrypted-Secret restore fixture remains authoritative.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from holm_persistence.backup_format import BACKUP_FILES, BACKUP_FORMAT_VERSION


def write_backup(root: Path, name: str, created_at: datetime) -> Path:
    destination = root / name
    destination.mkdir(mode=0o700)
    hashes: dict[str, str] = {}
    for member in sorted(BACKUP_FILES):
        content = f"FX-M7-08B-01-opaque-{member}".encode()
        (destination / member).write_bytes(content)
        (destination / member).chmod(0o400)
        hashes[member] = hashlib.sha256(content).hexdigest()
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "release": "FX-M7-08B-01",
        "migration_revision": "20260923_0030",
        "created_at": created_at.isoformat(),
        "sha256": hashes,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (destination / "manifest.json").chmod(0o400)
    return destination
