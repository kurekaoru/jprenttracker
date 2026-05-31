"""Structured event logging to SQLite — replaces freeform log file for INFO events."""
import sqlite3
from datetime import datetime, timezone

_DB_FILE: str | None = None


def configure(db_file: str):
    global _DB_FILE
    _DB_FILE = db_file


def ensure_table(con: sqlite3.Connection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT    NOT NULL,
            event  TEXT    NOT NULL,
            source TEXT,
            n      INTEGER,
            detail TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
    con.commit()


def log(event: str, source: str = None, n: int = None, detail: str = None,
        con: sqlite3.Connection = None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    _own = False
    if con is None:
        if not _DB_FILE:
            return
        con = sqlite3.connect(_DB_FILE, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        _own = True
    try:
        con.execute(
            "INSERT INTO events (ts, event, source, n, detail) VALUES (?,?,?,?,?)",
            (ts, event, source, n, detail),
        )
        con.commit()
    finally:
        if _own:
            con.close()


def prune(con: sqlite3.Connection, days: int = 30):
    con.execute(f"DELETE FROM events WHERE ts < datetime('now', '-{days} days')")
    con.commit()


# Human-readable formatting for /api/log
_TEMPLATES = {
    "startup":         lambda r: f"Scraper started  db={r['detail']}",
    "cycle_done":      lambda r: f"Cycle done — {r['detail']}  |  {r['n']} total listings",
    "new_listing":     lambda r: f"NEW [{(r['source'] or '?').upper()}]  {r['detail']}  ¥{r['n']:,}" if r['n'] else f"NEW [{(r['source'] or '?').upper()}]  {r['detail']}",
    "disappeared":     lambda r: f"{r['n']} listing(s) disappeared",
    "fetch_error":     lambda r: f"FETCH ERROR [{r['source']}]  {r['detail']}",
    "fetch_incomplete":lambda r: f"Fetch incomplete  [{r['source']}]",
    "completeness":    lambda r: f"Completeness check: {r['n']}/{r['detail']} fully scraped",
    "detail_scraped":  lambda r: f"Detail enriched [{r['source']}]: {r['n']}/{r['detail']} listings",
    "notif_sent":      lambda r: f"Notification sent [{r['source']}]  {r['detail']}",
    "notif_failed":    lambda r: f"Notification FAILED [{r['source']}]  {r['detail']}",
    "image_batch":     lambda r: f"Images downloaded: {r['n']} ({r['source']})",
    "geocode_batch":   lambda r: f"Geocoded: {r['n']} listings",
}


def format_row(row) -> str:
    ts = (row["ts"] or "")[:19].replace("T", " ")
    tpl = _TEMPLATES.get(row["event"])
    try:
        body = tpl(row) if tpl else f"{row['event']}  src={row['source']}  n={row['n']}  {row['detail'] or ''}"
    except Exception:
        body = f"{row['event']}  {row['detail'] or ''}"
    return f"[{ts}]  {body}"
