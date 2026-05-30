"""
JKK + UR Housing Monitor — orchestrator.

Runs all registered scrapers on a fixed poll interval, upserts listings,
sends notifications, and dispatches image collection to each scraper.

To add a new site:
  1. Create scraper_<site>.py with a class that subclasses BaseScraper.
  2. Import it here and append an instance to SCRAPERS.
"""

import json, time, os, hashlib, requests, sqlite3, threading, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from scraper_jkk   import JKKScraper
from scraper_ur    import URScraper
from scraper_suumo import SuumoScraper
from image_pipeline import download_pending_images, reset_asyncio_loop

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("jkk_monitor.log"),
        logging.StreamHandler(),
    ],
)
# httpcore/httpx log every request body including base64 image payloads at DEBUG —
# that fills the log file with hundreds of MB and causes the server to OOM when
# /api/log reads the whole file. Suppress them to WARNING.
for _noisy in ("httpcore", "httpx", "hpack", "anthropic._base_client"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
CONFIG_FILE       = "config.json"
DB_FILE           = "jkk_monitor.db"
POLL_INTERVAL     = 1800  # seconds

# ── Registered scrapers ───────────────────────────────────────────────────────
# Add new scrapers here — they are picked up automatically by the main loop.

SCRAPERS = [
    JKKScraper(DB_FILE),
    URScraper(DB_FILE),
    SuumoScraper(DB_FILE),
]

# ── DB helpers ────────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def listing_id(listing: dict) -> str:
    src = listing.get("source", "jkk")
    key = f"{src}-{listing.get('name','')}-{listing.get('address','')}-{listing.get('layout','')}-{listing.get('rent','')}"
    return hashlib.md5(key.encode()).hexdigest()


def init_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS listings ("
        "  id TEXT PRIMARY KEY,"
        "  name TEXT,"
        "  ward TEXT,"
        "  layout TEXT,"
        "  rent INTEGER,"
        "  size_m2 REAL,"
        "  url TEXT,"
        "  first_seen TEXT,"
        "  last_seen TEXT,"
        "  notified INTEGER DEFAULT 0"
        ");"
        "CREATE TABLE IF NOT EXISTS snapshots ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  listing_id TEXT,"
        "  rent INTEGER,"
        "  seen_at TEXT,"
        "  FOREIGN KEY(listing_id) REFERENCES listings(id)"
        ");"
        "CREATE TABLE IF NOT EXISTS listing_notifications ("
        "  listing_id TEXT NOT NULL,"
        "  user_id    INTEGER NOT NULL,"
        "  sent_at    TEXT DEFAULT (datetime('now')),"
        "  PRIMARY KEY (listing_id, user_id)"
        ");"
        "CREATE TABLE IF NOT EXISTS listing_images ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  listing_id  TEXT    NOT NULL,"
        "  url         TEXT    NOT NULL,"
        "  local_path  TEXT,"
        "  downloaded_at TEXT,"
        "  UNIQUE(listing_id, url)"
        ");"
        "CREATE TABLE IF NOT EXISTS route_cache ("
        "  origin      TEXT NOT NULL,"
        "  destination TEXT NOT NULL,"
        "  mode        TEXT NOT NULL,"
        "  response    TEXT NOT NULL,"
        "  cached_at   TEXT NOT NULL,"
        "  PRIMARY KEY (origin, destination, mode)"
        ");"
    )
    cols = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    for col, defn in [
        ("source",          "TEXT DEFAULT 'jkk'"),
        ("address",         "TEXT"),
        ("lat",             "REAL"),
        ("lng",             "REAL"),
        ("geocoded_at",     "TEXT"),
        ("disappeared_at",  "TEXT"),
        ("walk_min",        "INTEGER"),
        ("walk_m",          "INTEGER"),
        ("nearest_station", "TEXT"),
        ("thumbnail_url",   "TEXT"),
        ("floor_plan_data", "TEXT"),
        ("priority",        "TEXT"),
        ("building_type",   "TEXT"),
        ("appliances",      "TEXT"),
        ("detail_text",     "TEXT"),
        ("features",        "TEXT"),
        ("has_parking",     "INTEGER DEFAULT 0"),
        ("parking_fee",     "INTEGER DEFAULT 0"),
        ("age_years",       "INTEGER"),
        ("available_from",  "TEXT"),
        ("nearby_features", "TEXT"),
        ("prefecture",      "TEXT"),
        ("scrape_complete", "INTEGER DEFAULT 0"),
        ("data_issues",     "TEXT"),
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {defn}")

    img_cols = {row[1] for row in con.execute("PRAGMA table_info(listing_images)")}
    for col, defn in [
        ("image_type", "TEXT"),
        ("ocr_text",   "TEXT"),
    ]:
        if col not in img_cols:
            con.execute(f"ALTER TABLE listing_images ADD COLUMN {col} {defn}")

    con.commit()
    return con


def get_listing_row(con, lid):
    return con.execute("SELECT notified FROM listings WHERE id=?", (lid,)).fetchone()


def upsert_listing(con, lid: str, listing: dict, notified: bool = False) -> bool:
    now      = datetime.now().isoformat()
    source   = listing.get("source", "jkk")
    address  = listing.get("address") or listing.get("ward", "")
    thumb    = listing.get("thumbnail_url")
    priority      = listing.get("priority") or None
    building_type = listing.get("building_type") or None
    is_new   = not get_listing_row(con, lid)

    prefecture = listing.get("prefecture") or None

    if not is_new:
        con.execute(
            "UPDATE listings SET last_seen=?, rent=?, source=?, address=?, disappeared_at=NULL,"
            " notified=MAX(notified,?),"
            " prefecture=COALESCE(prefecture,?),"
            " priority=COALESCE(priority,?), building_type=COALESCE(building_type,?)"
            " WHERE id=?",
            (now, listing["rent"], source, address, int(notified),
             prefecture, priority, building_type, lid),
        )
    else:
        con.execute(
            "INSERT INTO listings "
            "(id,name,ward,layout,rent,size_m2,url,first_seen,last_seen,notified,"
            " source,address,thumbnail_url,priority,building_type,prefecture) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, listing["name"], listing["ward"], listing["layout"],
             listing["rent"], listing["size_m2"], listing["url"], now, now,
             int(notified), source, address, thumb, priority, building_type, prefecture),
        )
        if thumb:
            con.execute(
                "INSERT OR IGNORE INTO listing_images (listing_id, url) VALUES (?,?)",
                (lid, thumb),
            )
    con.execute(
        "INSERT INTO snapshots (listing_id,rent,seen_at) VALUES (?,?,?)",
        (lid, listing["rent"], now),
    )
    con.commit()
    return is_new


# ── Notifications ─────────────────────────────────────────────────────────────

SOURCE_META = {
    "jkk":   {"emoji": "🏠", "label": "JKK",   "footer": "JKK東京"},
    "ur":    {"emoji": "🏢", "label": "UR",     "footer": "UR都市機構"},
    "suumo": {"emoji": "🔑", "label": "SUUMO",  "footer": "SUUMO"},
}


def matches_criteria(listing: dict, config: dict) -> bool:
    wanted_wards = config.get("wards", [])
    if wanted_wards and listing.get("ward") not in wanted_wards:
        return False
    max_rent = config.get("max_rent", 0)
    if max_rent and listing.get("rent", 0) > max_rent:
        return False
    min_rent = config.get("min_rent", 0)
    if min_rent and listing.get("rent", 0) < min_rent:
        return False
    wanted_layouts = config.get("layouts", [])
    if wanted_layouts and listing.get("layout") not in wanted_layouts:
        return False
    min_size = config.get("min_size_m2", 0)
    if min_size and listing.get("size_m2", 0) < min_size:
        return False
    max_walk = config.get("max_walk_min", 0)
    if max_walk and listing.get("walk_min") and listing["walk_min"] > max_walk:
        return False
    if config.get("has_parking") == 1 and not listing.get("has_parking"):
        return False
    max_age = config.get("max_age_years", 0)
    if max_age and listing.get("age_years") and listing["age_years"] > max_age:
        return False
    required_features = config.get("required_features", [])
    if required_features:
        feature_str = listing.get("features") or ""
        for feat in required_features:
            if feat not in feature_str:
                return False
    if config.get("available_now") and not (listing.get("available_from") or "").startswith("随時"):
        return False
    filter_stations = config.get("filter_stations", [])
    if filter_stations and not config.get("wards"):
        ns = (listing.get("nearest_station") or "").rstrip("駅")
        if ns not in filter_stations:
            return False
    return True


def get_maps_url(name: str, ward: str) -> str:
    query = requests.utils.quote(f"{name} {ward} 東京")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def _slack_payload(listing: dict):
    meta = SOURCE_META.get(listing.get("source", "jkk"), SOURCE_META["jkk"])
    emoji, label, footer = meta["emoji"], meta["label"], meta["footer"]
    rent_str   = f"¥{listing['rent']:,}" if listing["rent"] else "要確認"
    size_str   = f"{listing['size_m2']} m²" if listing["size_m2"] else "—"
    maps_url   = get_maps_url(listing["name"], listing["ward"])
    detail_url = listing.get("url") or maps_url
    return {
        "text": f"{emoji} 新着{label}物件: {listing['name']} ({listing['ward']}) {rent_str}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
             "text": f"{emoji} 新着{label}物件が見つかりました！", "emoji": True}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*物件名* : {listing['name'] or '—'}"},
                {"type": "mrkdwn", "text": f"*所在地* : {listing['address'] or '—'}"},
                {"type": "mrkdwn", "text": f"*家賃* : {rent_str}"},
                {"type": "mrkdwn", "text": f"*間取り* : {listing['layout'] or '—'}"},
                {"type": "mrkdwn", "text": f"*専有面積* : {size_str}"},
            ]},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "🔗 物件ページ", "emoji": True},
                 "url": detail_url, "style": "primary"},
                {"type": "button", "text": {"type": "plain_text", "text": "📍 Google Maps", "emoji": True},
                 "url": maps_url},
            ]},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M')} | {footer}"}]},
        ],
    }, label


def _send_with_retry(fn, *args, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = fn(*args)
            if getattr(r, "status_code", None) == 429:
                time.sleep(int(r.headers.get("Retry-After", 1)))
                continue
            if hasattr(r, "raise_for_status"):
                r.raise_for_status()
            return True
        except Exception as e:
            log.error(f"Send failed (attempt {attempt+1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return False


def send_slack(listing: dict, webhook: str) -> bool:
    payload, label = _slack_payload(listing)
    ok = _send_with_retry(
        lambda: requests.post(webhook, json=payload, timeout=10)
    )
    if ok:
        log.info(f"Slack sent ✓  [{label}] {listing['name']}")
        time.sleep(1)
    return ok


def send_line(listing: dict, token: str) -> bool:
    meta = SOURCE_META.get(listing.get("source", "jkk"), SOURCE_META["jkk"])
    emoji, label = meta["emoji"], meta["label"]
    rent_str = f"¥{listing['rent']:,}" if listing["rent"] else "要確認"
    msg = (f"\n{emoji} 新着{label}物件\n物件名: {listing['name'] or '—'}\n"
           f"所在地: {listing['ward']}\n家賃: {rent_str}\n"
           f"間取り: {listing['layout'] or '—'}\n面積: {listing.get('size_m2') or '—'} m²")
    if listing.get("url"):
        msg += f"\n{listing['url']}"
    ok = _send_with_retry(
        lambda: requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {token}"},
            data={"message": msg}, timeout=10,
        )
    )
    if ok:
        log.info(f"LINE sent ✓  [{label}] {listing['name']}")
        time.sleep(1)
    return ok


def send_email(listing: dict, to_email: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not smtp_host or not smtp_user:
        log.warning("SMTP not configured — skipping email")
        return False
    meta = SOURCE_META.get(listing.get("source", "jkk"), SOURCE_META["jkk"])
    emoji, label = meta["emoji"], meta["label"]
    rent_str = f"¥{listing['rent']:,}" if listing["rent"] else "要確認"
    subject  = f"{emoji} 新着{label}物件: {listing['name']} ({listing['ward']}) {rent_str}"
    body = (f"新着{label}物件が見つかりました！\n\n物件名: {listing['name'] or '—'}\n"
            f"所在地: {listing['ward']}\n家賃: {rent_str}\n"
            f"間取り: {listing['layout'] or '—'}\n面積: {listing.get('size_m2') or '—'} m²\n")
    if listing.get("url"):
        body += f"\n物件ページ: {listing['url']}"
    body += f"\n\n検出日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"]   = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        log.info(f"Email sent ✓  [{label}] {listing['name']} → {to_email}")
        return True
    except Exception as e:
        log.error(f"Email send failed to {to_email}: {e}")
        return False


def send_telegram(listing: dict, target: str) -> bool:
    parts = target.split("|", 1)
    if len(parts) != 2:
        log.error(f"Telegram target malformed: {target}")
        return False
    token, chat_id = parts
    meta = SOURCE_META.get(listing.get("source", "jkk"), SOURCE_META["jkk"])
    emoji, label = meta["emoji"], meta["label"]
    rent_str = f"¥{listing['rent']:,}" if listing["rent"] else "要確認"
    text = (f"{emoji} *新着{label}物件*\n物件名: {listing['name'] or '—'}\n"
            f"所在地: {listing['ward']}\n家賃: {rent_str}\n"
            f"間取り: {listing['layout'] or '—'}\n面積: {listing.get('size_m2') or '—'} m²")
    if listing.get("url"):
        text += f"\n[物件ページ]({listing['url']})"
    ok = _send_with_retry(
        lambda: requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    )
    if ok:
        log.info(f"Telegram sent ✓  [{label}] {listing['name']}")
    return ok


def get_user_targets(con):
    cur = con.execute("""
        SELECT un.id AS notif_id, un.user_id, un.type, un.target,
               COALESCE(us.min_rent,          0)    AS min_rent,
               COALESCE(us.max_rent,          0)    AS max_rent,
               COALESCE(us.min_size_m2,       0)    AS min_size_m2,
               COALESCE(us.max_walk_min,      0)    AS max_walk_min,
               COALESCE(us.layouts,       '[]')     AS layouts,
               COALESCE(us.wards,         '[]')     AS wards,
               COALESCE(us.has_parking,     NULL)   AS has_parking,
               COALESCE(us.max_age_years,     0)    AS max_age_years,
               COALESCE(us.required_features,'[]')  AS required_features,
               COALESCE(us.filter_stations,  '[]')  AS filter_stations,
               COALESCE(us.available_now,      0)   AS available_now
          FROM user_notifications un
          LEFT JOIN user_settings us ON us.user_id = un.user_id
         WHERE un.enabled = 1
    """)
    cols = [d[0] for d in cur.description]
    result = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["layouts"]           = json.loads(d.get("layouts")           or "[]")
        d["wards"]             = json.loads(d.get("wards")             or "[]")
        d["required_features"] = json.loads(d.get("required_features") or "[]")
        d["filter_stations"]   = json.loads(d.get("filter_stations")   or "[]")
        result.append(d)
    return result


def already_notified_user(con, listing_id, user_id):
    return con.execute(
        "SELECT 1 FROM listing_notifications WHERE listing_id=? AND user_id=?",
        (listing_id, user_id),
    ).fetchone() is not None


def mark_notified_user(con, listing_id, user_id):
    con.execute(
        "INSERT OR IGNORE INTO listing_notifications (listing_id, user_id) VALUES (?,?)",
        (listing_id, user_id),
    )
    con.commit()


def send_notifications(con, listing: dict, lid: str, global_config: dict) -> bool:
    targets  = get_user_targets(con)
    notified = False

    if targets:
        for t in targets:
            if already_notified_user(con, lid, t["user_id"]):
                continue
            if not matches_criteria(listing, t):
                continue
            sent = False
            if t["type"] == "slack":
                sent = send_slack(listing, t["target"])
            elif t["type"] == "line":
                sent = send_line(listing, t["target"])
            elif t["type"] == "telegram":
                sent = send_telegram(listing, t["target"])
            elif t["type"] == "email":
                sent = send_email(listing, t["target"])
            if sent:
                mark_notified_user(con, lid, t["user_id"])
                notified = True
    else:
        webhook = SLACK_WEBHOOK_URL or global_config.get("slack_webhook", "")
        if webhook:
            notified = send_slack(listing, webhook)

    return notified


# ── Module-level helpers (used by server.py) ──────────────────────────────────

def backfill_images(listing_ids: list[str] | None = None, limit: int = 50) -> None:
    """Backfill UR images. Called by server.py /api/images/backfill endpoint."""
    scraper = next((s for s in SCRAPERS if s.source == "ur"), None)
    if scraper:
        scraper.backfill_images(listing_ids=listing_ids, limit=limit)


# ── Main loop ─────────────────────────────────────────────────────────────────

def update_scrape_completeness(con):
    """Re-evaluate scrape_complete for all non-disappeared listings and persist findings."""
    cur = con.execute("""
        SELECT l.id, l.source, l.lat, l.lng, l.detail_text, l.age_years, l.floor_plan_data,
               (SELECT COUNT(*) FROM listing_images li
                WHERE li.listing_id=l.id AND li.local_path IS NOT NULL
                  AND li.image_type != 'floor_plan') as photo_count,
               (SELECT COUNT(*) FROM listing_images li
                WHERE li.listing_id=l.id AND li.local_path IS NOT NULL
                  AND li.image_type = 'floor_plan') as fp_img_count
        FROM listings l
        WHERE l.disappeared_at IS NULL
    """)
    rows = cur.fetchall()
    updates = []
    for r in rows:
        issues = []
        if not r["lat"] or not r["lng"]:
            issues.append("no_geocoding")
        if r["photo_count"] == 0:
            issues.append("no_photos")
        has_fp = bool(r["floor_plan_data"]) or r["fp_img_count"] > 0
        if not has_fp:
            issues.append("no_floor_plan")
        if not r["detail_text"] or len(r["detail_text"]) < 100:
            issues.append("no_detail_text")
        if r["age_years"] is None:
            issues.append("no_age_years")
        complete = 1 if not issues else 0
        issues_str = ",".join(issues) if issues else None
        updates.append((complete, issues_str, r["id"]))
    for upd in updates:
        con.execute("UPDATE listings SET scrape_complete=?, data_issues=? WHERE id=?", upd)
    con.commit()
    done  = sum(1 for c, _, _ in updates if c == 1)
    total = len(updates)
    log.info(f"Completeness: {done}/{total} listings fully scraped.")


def update_listing_completeness(con, lid: str) -> None:
    """Re-evaluate scrape_complete for a single listing (called on-the-fly after detail scrape)."""
    r = con.execute("""
        SELECT l.lat, l.lng, l.detail_text, l.age_years, l.floor_plan_data,
               (SELECT COUNT(*) FROM listing_images li
                WHERE li.listing_id=l.id AND li.local_path IS NOT NULL
                  AND li.image_type != 'floor_plan') as photo_count,
               (SELECT COUNT(*) FROM listing_images li
                WHERE li.listing_id=l.id AND li.local_path IS NOT NULL
                  AND li.image_type = 'floor_plan') as fp_img_count
        FROM listings l WHERE l.id=?
    """, (lid,)).fetchone()
    if not r:
        return
    issues = []
    if not r["lat"] or not r["lng"]:         issues.append("no_geocoding")
    if r["photo_count"] == 0:                issues.append("no_photos")
    if not (r["floor_plan_data"] or r["fp_img_count"] > 0): issues.append("no_floor_plan")
    if not r["detail_text"] or len(r["detail_text"]) < 100: issues.append("no_detail_text")
    if r["age_years"] is None:               issues.append("no_age_years")
    complete   = 1 if not issues else 0
    issues_str = ",".join(issues) if issues else None
    con.execute("UPDATE listings SET scrape_complete=?, data_issues=? WHERE id=?",
                (complete, issues_str, lid))
    con.commit()


def run():
    log.info("JKK + UR Monitor started ✓")
    con = init_db()
    log.info(f"Database: {DB_FILE}")

    # Build source → scraper index for image dispatch
    scraper_by_source = {s.source: s for s in SCRAPERS}
    _cycle = 0

    while True:
        _cycle += 1
        config = load_json(CONFIG_FILE, {})

        all_listings   = []
        sources_ok     = []

        for scraper in SCRAPERS:
            reset_asyncio_loop()
            try:
                listings, ok = scraper.fetch_listings(config)
                all_listings.extend(listings)
                if ok:
                    sources_ok.append(scraper.source)
                else:
                    log.warning(f"{scraper.source} fetch did not complete — "
                                "skipping disappearance detection for this source.")
            except Exception as e:
                log.error(f"{scraper.source} fetch failed: {e}")

        # Upsert all listings and track new ones per source
        new_count       = {s.source: 0 for s in SCRAPERS}
        new_lids_by_src: dict[str, list[str]] = {}

        for listing in all_listings:
            lid    = listing_id(listing)
            row    = get_listing_row(con, lid)
            already_notified = bool(row[0]) if row else False
            notified = False

            if not already_notified:
                src = listing.get("source", "jkk").upper()
                rent_display = f"¥{listing['rent']:,}" if listing["rent"] else "不明"
                log.info(f"NEW [{src}] {listing['name']} | {listing['ward']} | {rent_display}")
                notified = send_notifications(con, listing, lid, config)
                new_count[listing.get("source", "jkk")] = (
                    new_count.get(listing.get("source", "jkk"), 0) + 1
                )

            is_new = upsert_listing(con, lid, listing, notified=notified)
            if is_new:
                src = listing.get("source", "jkk")
                new_lids_by_src.setdefault(src, []).append(lid)

        # Mark disappeared for sources whose fetch succeeded
        if sources_ok:
            now_iso  = datetime.now().isoformat()
            cutoff   = (datetime.now() - timedelta(seconds=int(POLL_INTERVAL * 1.5))).isoformat()
            phs      = ",".join("?" * len(sources_ok))
            cur      = con.execute(
                f"UPDATE listings SET disappeared_at=? "
                f"WHERE disappeared_at IS NULL AND last_seen < ? AND source IN ({phs})",
                [now_iso, cutoff] + sources_ok,
            )
            con.commit()
            if cur.rowcount:
                log.info(f"{cur.rowcount} listing(s) marked as disappeared.")

        total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        counts = " | ".join(f"{new_count.get(s.source,0)} new {s.source.upper()}" for s in SCRAPERS)
        log.info(f"Cycle done — {counts} | {total} total. Next check in {POLL_INTERVAL//60}m.")

        # Catch-up downloader for any already-known missing images
        download_pending_images(con)

        # Background image collection — one thread per scraper that has new listings
        for src, lids in new_lids_by_src.items():
            scraper = scraper_by_source.get(src)
            if scraper and lids:
                threading.Thread(
                    target=scraper.fetch_images_batch,
                    args=(lids,),
                    daemon=True,
                ).start()

        # Every 4 cycles (~2h), backfill UR listings that still have no images
        if _cycle % 4 == 0:
            ur_scraper = scraper_by_source.get("ur")
            if ur_scraper:
                threading.Thread(
                    target=ur_scraper.backfill_images,
                    kwargs={"limit": 20},
                    daemon=True,
                ).start()

        # Re-evaluate completeness after images/data have had time to land
        update_scrape_completeness(con)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
