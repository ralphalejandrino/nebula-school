"""A daily copy of Nebula's database. Your notes, answers and approvals exist nowhere else.

Taken with `VACUUM INTO` (a consistent copy even mid-write), at most once per day, at the end
of a sweep; the newest KEEP copies are kept. 0600 — the database holds personal mail, so the
copies stay on this machine, next to it (review 2026-10-12).
🔴 Not `Connection.backup()`: it retries FOREVER while the database is busy, so one long
write would have hung the sweep. VACUUM INTO honours the busy timeout and raises instead.
"""
import os
import sqlite3
from pathlib import Path

from django.conf import settings
from django.utils import timezone

KEEP = 14


def daily(keep=KEEP):
    """Back up once per local day. Returns the new file, or None if today's exists."""
    from django.db import connection
    name = str(connection.settings_dict["NAME"])
    if name.startswith("file:") or ":memory:" in name:
        return None                                 # an in-memory (test) database: nothing to keep
    db = Path(name)
    out_dir = settings.DATA_DIR / "backups"
    out_dir.mkdir(mode=0o700, exist_ok=True)
    dest = out_dir / f"nebula-{timezone.localtime():%Y-%m-%d}.sqlite3"
    if dest.exists():
        return None
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)                   # VACUUM INTO refuses an existing file
    src = sqlite3.connect(str(db), timeout=30)
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()
    os.chmod(tmp, 0o600)
    tmp.rename(dest)                         # never leave a half-written "backup" behind
    for old in sorted(out_dir.glob("nebula-*.sqlite3"))[:-keep]:
        old.unlink()
    return dest
