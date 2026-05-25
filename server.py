"""
Config + read API for the dashboard. Run alongside scraper4.py.
"""

from flask import Flask, jsonify, request, redirect, send_file
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity
)
from werkzeug.security import generate_password_hash, check_password_hash
import json, logging, math, os, secrets, sqlite3, time, threading, requests
from datetime import datetime, timedelta
from urllib.parse import urlencode
from dotenv import load_dotenv

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

load_dotenv()

CONFIG_FILE = "config.json"
LOG_FILE    = "jkk_monitor.log"
DB_FILE     = "jkk_monitor.db"

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=30)
jwt = JWTManager(app)

LINE_CLIENT_ID     = os.environ.get("LINE_NOTIFY_CLIENT_ID", "")
LINE_CLIENT_SECRET = os.environ.get("LINE_NOTIFY_CLIENT_SECRET", "")
LINE_REDIRECT_URI  = os.environ.get("LINE_NOTIFY_REDIRECT_URI", "")

SLACK_CLIENT_ID     = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
SLACK_REDIRECT_URI  = os.environ.get("SLACK_REDIRECT_URI", "")

TELEGRAM_BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_MAPS_SERVER_KEY  = os.environ.get("GOOGLE_MAPS_SERVER_KEY", "")

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI",
                         "https://jkk-monitor.duckdns.org/api/auth/google/callback")

GITHUB_CLIENT_ID     = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI  = os.environ.get("GITHUB_REDIRECT_URI",
                         "https://jkk-monitor.duckdns.org/api/auth/github/callback")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://34.72.39.84:5050")

TOKYO_WARDS = [
    "千代田区","中央区","港区","新宿区","文京区","台東区","墨田区","江東区",
    "品川区","目黒区","大田区","世田谷区","渋谷区","中野区","杉並区","豊島区",
    "北区","荒川区","板橋区","練馬区","足立区","葛飾区","江戸川区"
]

LAYOUTS = ["1R","1K","1DK","1LDK","2K","2DK","2LDK","3K","3DK","3LDK","4LDK以上"]

WARD_CENTROID = {
    "千代田区":(35.6940,139.7536),"中央区":(35.6701,139.7724),"港区":(35.6580,139.7515),
    "新宿区":(35.6938,139.7035),"文京区":(35.7081,139.7522),"台東区":(35.7128,139.7800),
    "墨田区":(35.7106,139.8016),"江東区":(35.6731,139.8174),"品川区":(35.6092,139.7302),
    "目黒区":(35.6411,139.6982),"大田区":(35.5614,139.7161),"世田谷区":(35.6464,139.6532),
    "渋谷区":(35.6640,139.6982),"中野区":(35.7073,139.6638),"杉並区":(35.6995,139.6364),
    "豊島区":(35.7264,139.7160),"北区":(35.7528,139.7336),"荒川区":(35.7361,139.7831),
    "板橋区":(35.7512,139.7094),"練馬区":(35.7357,139.6517),"足立区":(35.7754,139.8044),
    "葛飾区":(35.7434,139.8473),"江戸川区":(35.7066,139.8683),
    "八王子市":(35.6664,139.3157),"立川市":(35.7141,139.4078),"武蔵野市":(35.7180,139.5666),
    "三鷹市":(35.6837,139.5599),"府中市":(35.6691,139.4778),"昭島市":(35.7058,139.3593),
    "調布市":(35.6505,139.5403),"町田市":(35.5460,139.4470),"小金井市":(35.6997,139.5061),
    "小平市":(35.7283,139.4773),"日野市":(35.6716,139.3953),"東村山市":(35.7546,139.4684),
    "国分寺市":(35.7106,139.4622),"国立市":(35.6837,139.4413),"福生市":(35.7383,139.3266),
    "狛江市":(35.6346,139.5786),"清瀬市":(35.7858,139.5263),"東久留米市":(35.7585,139.5290),
    "武蔵村山市":(35.7549,139.3878),"多摩市":(35.6363,139.4463),"稲城市":(35.6379,139.5040),
    "羽村市":(35.7669,139.3115),"西東京市":(35.7257,139.5384),
    "横浜市鶴見区":   (35.5084,139.6761),"横浜市神奈川区":(35.4890,139.6339),
    "横浜市西区":     (35.4666,139.6218),"横浜市中区":    (35.4437,139.6427),
    "横浜市南区":     (35.4255,139.6145),"横浜市保土ケ谷区":(35.4607,139.5952),
    "横浜市磯子区":   (35.3990,139.6327),"横浜市金沢区":  (35.3534,139.6284),
    "横浜市港北区":   (35.5300,139.6305),"横浜市戸塚区":  (35.3975,139.5332),
    "横浜市港南区":   (35.3953,139.5965),"横浜市旭区":    (35.4621,139.5592),
    "横浜市緑区":     (35.5100,139.5872),"横浜市瀬谷区":  (35.4630,139.5089),
    "横浜市栄区":     (35.3679,139.5737),"横浜市青葉区":  (35.5560,139.5479),
    "横浜市都筑区":   (35.5399,139.5763),
    "川崎市川崎区":   (35.5308,139.6974),"川崎市幸区":    (35.5389,139.6726),
    "川崎市中原区":   (35.5731,139.6615),"川崎市高津区":  (35.6020,139.6430),
    "川崎市麻生区":   (35.6451,139.4998),"川崎市多摩区":  (35.6118,139.5505),
    "川崎市宮前区":   (35.5885,139.5742),
    "相模原市緑区":   (35.5968,139.3906),"相模原市中央区":(35.5716,139.3720),
    "相模原市南区":   (35.5298,139.3888),
}

GEOCODE_TIMEOUT_S      = 5
GEOCODE_DELAY_S        = 0.3
GEOCODE_LOOKBACK_HOURS = 6
GSI_URL        = "https://msearch.gsi.go.jp/address-search/AddressSearch"
HEARTRAILS_URL = "https://express.heartrails.com/api/json"
HEARTRAILS_TIMEOUT_S = 5

_geo_lock    = threading.Lock()
_geo_running = False
_tg_poll_lock = threading.Lock()


# ── DB ────────────────────────────────────────────────────────────────────────

def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row

    # Listings table migrations (idempotent)
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
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {defn}")

    # listing_images column migrations
    img_cols = {row[1] for row in con.execute("PRAGMA table_info(listing_images)")}
    for col, defn in [("image_type", "TEXT"), ("ocr_text", "TEXT")]:
        if img_cols and col not in img_cols:
            con.execute(f"ALTER TABLE listing_images ADD COLUMN {col} {defn}")

    con.executescript("""
        CREATE TABLE IF NOT EXISTS listing_images (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id   TEXT    NOT NULL,
            url          TEXT    NOT NULL,
            local_path   TEXT,
            downloaded_at TEXT,
            image_type   TEXT,
            ocr_text     TEXT,
            UNIQUE(listing_id, url)
        );
        CREATE TABLE IF NOT EXISTS listing_embeddings (
            listing_id  TEXT PRIMARY KEY,
            embedding   BLOB NOT NULL,
            model       TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)

    # User / auth tables
    con.executescript("""
        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name  TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id      INTEGER PRIMARY KEY REFERENCES users(id),
            min_rent     INTEGER DEFAULT 0,
            max_rent     INTEGER DEFAULT 0,
            min_size_m2  REAL    DEFAULT 0,
            max_walk_min INTEGER DEFAULT 0,
            layouts      TEXT    DEFAULT '[]',
            wards        TEXT    DEFAULT '[]',
            updated_at   TEXT    DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id),
            type       TEXT NOT NULL,
            target     TEXT NOT NULL,
            label      TEXT,
            enabled    INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS listing_notifications (
            listing_id TEXT NOT NULL,
            user_id    INTEGER NOT NULL,
            sent_at    TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (listing_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS listing_facilities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id  TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            name        TEXT,
            lat         REAL    NOT NULL,
            lng         REAL    NOT NULL,
            distance_m  INTEGER NOT NULL,
            fetched_at  TEXT    DEFAULT (datetime('now')),
            UNIQUE(listing_id, lat, lng)
        );
        CREATE INDEX IF NOT EXISTS idx_lf_listing ON listing_facilities(listing_id);
    """)
    con.commit()

    # New activity tables (idempotent)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER REFERENCES users(id),
            title      TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER REFERENCES chat_sessions(id) ON DELETE CASCADE,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            listing_ids TEXT DEFAULT '[]',
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_messages(session_id);
    """)
    con.commit()

    con.executescript("""
        CREATE TABLE IF NOT EXISTS user_saved_listings (
            user_id    INTEGER NOT NULL REFERENCES users(id),
            listing_id TEXT    NOT NULL,
            saved_at   TEXT    DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, listing_id)
        );
        CREATE TABLE IF NOT EXISTS user_listing_views (
            user_id     INTEGER NOT NULL REFERENCES users(id),
            listing_id  TEXT    NOT NULL,
            last_viewed TEXT    DEFAULT (datetime('now')),
            view_count  INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, listing_id)
        );
    """)
    con.commit()

    # listings floor_plan_data column
    try:
        con.execute("ALTER TABLE listings ADD COLUMN floor_plan_data TEXT")
        con.commit()
    except Exception:
        pass

    # users table migrations
    for col in ("google_id", "github_id", "avatar_url"):
        try:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            con.commit()
        except Exception:
            pass

    # Column migrations (idempotent)
    for col, defn in [
        ("map_layers",     "TEXT DEFAULT '{}'"),
        ("lang",           "TEXT DEFAULT 'ja'"),
        ("commute_dests",  "TEXT DEFAULT '[]'"),
        ("ai_profile",     "TEXT DEFAULT '{}'"),
        ("personal_info",  "TEXT DEFAULT '{}'"),
    ]:
        try:
            con.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {defn}")
            con.commit()
        except Exception:
            pass

    # Migration: clear facility cache if subway_entrance noise is present
    bad = con.execute(
        "SELECT COUNT(*) FROM listing_facilities WHERE type='station' "
        "AND (name GLOB '[0-9]*' OR name GLOB '[0-9]' "
        "     OR name LIKE '%exit%' OR name LIKE '%entrance%' OR name='station')"
    ).fetchone()[0]
    if bad > 0:
        con.execute("DELETE FROM listing_facilities")
        con.commit()
        logging.info(f"DB migration: cleared facility cache ({bad} bad station entries)")

    # JWT secret — stored in DB so it survives server restarts
    row = con.execute("SELECT value FROM app_config WHERE key='jwt_secret'").fetchone()
    if row:
        app.config["JWT_SECRET_KEY"] = row["value"]
    else:
        secret = secrets.token_hex(32)
        con.execute("INSERT INTO app_config (key, value) VALUES ('jwt_secret', ?)", (secret,))
        con.commit()
        app.config["JWT_SECRET_KEY"] = secret

    return con


# ── Geocoding helpers ─────────────────────────────────────────────────────────

def _gsi_geocode(query):
    try:
        r = requests.get(GSI_URL, params={"q": query}, timeout=GEOCODE_TIMEOUT_S,
                         headers={"User-Agent": "jkktrackr/1.0 (dashboard)"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    if not data:
        return None
    try:
        lng, lat = data[0]["geometry"]["coordinates"]
    except (KeyError, IndexError, TypeError):
        return None
    if not (35.0 <= lat <= 36.5 and 138.5 <= lng <= 140.5):
        return None
    return (float(lat), float(lng))


def _nearest_station(lat, lng):
    try:
        r = requests.get(HEARTRAILS_URL,
                         params={"method": "getStations", "x": str(lng), "y": str(lat)},
                         timeout=HEARTRAILS_TIMEOUT_S,
                         headers={"User-Agent": "jkktrackr/1.0 (dashboard)"})
        r.raise_for_status()
        stations = r.json().get("response", {}).get("station") or []
        if not stations:
            return None
        s        = stations[0]
        dist_raw = "".join(c for c in str(s.get("distance", "0")) if c.isdigit())
        dist_m   = int(dist_raw) if dist_raw else 0
        walk_min = max(1, math.ceil(dist_m / 80))
        name     = s.get("name", "")
        line     = s.get("line", "")
        label    = f"{name}（{line}）" if line else name
        return (label, walk_min, dist_m)
    except Exception:
        return None


_KANAGAWA_PREFIXES = ("横浜市", "川崎市", "相模原市")

def _prefecture(ward):
    if any(ward.startswith(p) for p in _KANAGAWA_PREFIXES):
        return "神奈川県"
    return "東京都"

def _build_geocode_query(row):
    name    = (row["name"]    or "").strip()
    address = (row["address"] or "").strip()
    ward    = (row["ward"]    or "").strip()
    pref    = _prefecture(ward)
    if address and address != ward:
        return f"{pref}{address}"
    if name:
        return f"{pref}{ward} {name}"
    return f"{pref}{ward}"


OVERPASS_URL = "https://overpass-api.de/api/interpreter"

_FACILITY_TYPES = [
    ("shop",    "convenience",     "konbini"),
    ("shop",    "supermarket",     "supermarket"),
    ("amenity", "kindergarten",    "kindergarten"),
    ("amenity", "school",          "school"),
    ("amenity", "hospital",        "hospital"),
    ("amenity", "clinic",          "clinic"),
    ("amenity", "doctors",         "clinic"),
    ("amenity", "pharmacy",        "pharmacy"),
    ("leisure", "park",            "park"),
    ("railway", "station",         "station"),
]

def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def _fetch_and_store_facilities(listing_id, lat, lng, con):
    radius = 1000
    lines  = "\n".join(
        f'  node["{k}"="{v}"](around:{radius},{lat},{lng});'
        for k, v, _ in _FACILITY_TYPES
    )
    query = f"[out:json][timeout:25];\n(\n{lines}\n);\nout body;"
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=35,
                          headers={"User-Agent": "jkktrackr/1.0"})
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except Exception as e:
        logging.warning(f"Overpass fetch failed for {listing_id}: {e}")
        return

    rows = {}
    for el in elements:
        elat, elng = el.get("lat"), el.get("lon")
        if not elat or not elng:
            continue
        tags = el.get("tags", {})
        category = next(
            (cat for k, v, cat in _FACILITY_TYPES if tags.get(k) == v), None
        )
        if not category:
            continue
        name = (tags.get("name") or tags.get("name:ja")
                or tags.get("name:en") or category)
        dist = _haversine_m(lat, lng, elat, elng)
        key  = (round(elat, 6), round(elng, 6))
        if key not in rows:
            rows[key] = (listing_id, category, name, elat, elng, dist)

    # Deduplicate stations by name: keep only the nearest node per station name
    station_by_name: dict = {}
    deduped = {}
    for key, row in rows.items():
        if row[1] == 'station':
            sname = row[2]
            if sname not in station_by_name or row[5] < station_by_name[sname][1][5]:
                station_by_name[sname] = (key, row)
        else:
            deduped[key] = row
    for key, row in station_by_name.values():
        deduped[key] = row
    rows = deduped

    for row in rows.values():
        con.execute(
            "INSERT OR IGNORE INTO listing_facilities "
            "(listing_id, type, name, lat, lng, distance_m) VALUES (?,?,?,?,?,?)",
            row,
        )
    logging.info(f"Facilities: {len(rows)} POIs stored for {listing_id}")


def _geocode_worker():
    global _geo_running
    try:
        while True:
            con = sqlite3.connect(DB_FILE)
            con.row_factory = sqlite3.Row
            cutoff = (datetime.now() - timedelta(hours=GEOCODE_LOOKBACK_HOURS)).isoformat()

            row = con.execute(
                "SELECT id, name, ward, COALESCE(address, ward) AS address "
                "  FROM listings "
                " WHERE last_seen >= ? AND lat IS NULL AND geocoded_at IS NULL "
                " LIMIT 1",
                (cutoff,),
            ).fetchone()
            if row:
                coords = _gsi_geocode(_build_geocode_query(row))
                if coords is None:
                    coords = WARD_CENTROID.get(row["ward"])
                now = datetime.now().isoformat()
                if coords is not None:
                    station  = _nearest_station(coords[0], coords[1])
                    con.execute(
                        "UPDATE listings SET lat=?,lng=?,geocoded_at=?,nearest_station=?,walk_min=?,walk_m=? WHERE id=?",
                        (coords[0], coords[1], now,
                         station[0] if station else None,
                         station[1] if station else None,
                         station[2] if station else None,
                         row["id"]),
                    )
                else:
                    con.execute("UPDATE listings SET geocoded_at=? WHERE id=?", (now, row["id"]))
                con.commit()
                con.close()
                time.sleep(GEOCODE_DELAY_S)
                continue

            row = con.execute(
                "SELECT id, lat, lng FROM listings "
                " WHERE last_seen >= ? AND lat IS NOT NULL AND (nearest_station IS NULL OR nearest_station = '') "
                " LIMIT 1",
                (cutoff,),
            ).fetchone()
            if row:
                station = _nearest_station(row["lat"], row["lng"])
                if station:
                    con.execute("UPDATE listings SET nearest_station=?, walk_min=?, walk_m=? WHERE id=?",
                                (station[0], station[1], station[2], row["id"]))
                else:
                    con.execute("UPDATE listings SET nearest_station='' WHERE id=?", (row["id"],))
                con.commit()
                con.close()
                time.sleep(GEOCODE_DELAY_S)
                continue

            # Phase 3: fetch Overpass facilities for geocoded listings that have none yet
            row = con.execute(
                "SELECT id, lat, lng FROM listings "
                " WHERE last_seen >= ? AND lat IS NOT NULL "
                "   AND id NOT IN (SELECT DISTINCT listing_id FROM listing_facilities) "
                " LIMIT 1",
                (cutoff,),
            ).fetchone()
            if row:
                _fetch_and_store_facilities(row["id"], row["lat"], row["lng"], con)
                con.commit()
                con.close()
                time.sleep(1.5)
                continue

            con.close()
            return
    except Exception:
        pass
    finally:
        with _geo_lock:
            _geo_running = False


def _ensure_geocoding():
    global _geo_running
    with _geo_lock:
        if _geo_running:
            return
        _geo_running = True
    threading.Thread(target=_geocode_worker, daemon=True).start()


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.route("/api/auth/register", methods=["POST"])
def register():
    data         = request.get_json() or {}
    email        = data.get("email", "").strip().lower()
    password     = data.get("password", "")
    display_name = data.get("display_name", "").strip()

    if not email or not password:
        return jsonify({"error": "メールアドレスとパスワードを入力してください"}), 400
    if len(password) < 8:
        return jsonify({"error": "パスワードは8文字以上にしてください"}), 400

    # Optional demographics — stored in ai_profile for future ML use
    ai_profile = {}
    for key in ("household", "age_range", "income_bracket"):
        val = data.get(key, "").strip()
        if val:
            ai_profile[key] = val
    lang = data.get("lang", "ja")

    con = db()
    try:
        con.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
            (email, generate_password_hash(password), display_name or email.split("@")[0])
        )
        con.commit()
        user = con.execute("SELECT id, email, display_name FROM users WHERE email=?", (email,)).fetchone()
        con.execute("""
            INSERT INTO user_settings (user_id, lang, ai_profile)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                lang=excluded.lang, ai_profile=excluded.ai_profile, updated_at=datetime('now')
        """, (user["id"], lang, json.dumps(ai_profile)))
        con.commit()
        token = create_access_token(identity=str(user["id"]))
        return jsonify({"token": token, "email": user["email"], "display_name": user["display_name"]})
    except sqlite3.IntegrityError:
        return jsonify({"error": "このメールアドレスはすでに登録されています"}), 409
    finally:
        con.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    data     = request.get_json() or {}
    email    = data.get("email", "").strip().lower()
    password = data.get("password", "")

    con  = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    con.close()

    if not user:
        return jsonify({"error": "メールアドレスまたはパスワードが間違っています"}), 401
    if not user["password_hash"]:
        return jsonify({"error": "このアカウントはGoogleログイン専用です"}), 401
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "メールアドレスまたはパスワードが間違っています"}), 401

    token = create_access_token(identity=str(user["id"]))
    return jsonify({"token": token, "email": user["email"], "display_name": user["display_name"]})


@app.route("/api/auth/me")
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    con     = db()
    user    = con.execute(
        "SELECT id, email, display_name, avatar_url, created_at FROM users WHERE id=?", (user_id,)
    ).fetchone()
    con.close()
    if not user:
        return jsonify({"error": "ユーザーが見つかりません"}), 404
    return jsonify(dict(user))


@app.route("/api/auth/change-password", methods=["POST"])
@jwt_required()
def change_password():
    user_id  = int(get_jwt_identity())
    data     = request.get_json() or {}
    current  = data.get("current_password", "")
    new_pw   = data.get("new_password", "")

    if len(new_pw) < 8:
        return jsonify({"error": "新しいパスワードは8文字以上にしてください"}), 400

    con  = db()
    user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current):
        con.close()
        return jsonify({"error": "現在のパスワードが間違っています"}), 401

    con.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pw), user_id))
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── User settings ─────────────────────────────────────────────────────────────

@app.route("/api/user/settings", methods=["GET"])
@jwt_required()
def get_user_settings():
    user_id = int(get_jwt_identity())
    con     = db()
    row     = con.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    if not row:
        return jsonify({})
    d = dict(row)
    d["layouts"] = json.loads(d.get("layouts") or "[]")
    d["wards"]   = json.loads(d.get("wards")   or "[]")
    return jsonify(d)


@app.route("/api/user/settings", methods=["POST"])
@jwt_required()
def set_user_settings():
    user_id = int(get_jwt_identity())
    data    = request.get_json() or {}
    con     = db()
    con.execute("""
        INSERT INTO user_settings (user_id, min_rent, max_rent, min_size_m2, max_walk_min, layouts, wards)
        VALUES (:uid, :min_rent, :max_rent, :min_size, :max_walk, :layouts, :wards)
        ON CONFLICT(user_id) DO UPDATE SET
            min_rent=excluded.min_rent, max_rent=excluded.max_rent,
            min_size_m2=excluded.min_size_m2, max_walk_min=excluded.max_walk_min,
            layouts=excluded.layouts, wards=excluded.wards,
            updated_at=datetime('now')
    """, {
        "uid":      user_id,
        "min_rent": int(data.get("min_rent") or 0),
        "max_rent": int(data.get("max_rent") or 0),
        "min_size": float(data.get("min_size_m2") or 0),
        "max_walk": int(data.get("max_walk_min") or 0),
        "layouts":  json.dumps(data.get("layouts") or []),
        "wards":    json.dumps(data.get("wards")   or []),
    })
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── Map layer preferences ─────────────────────────────────────────────────────

@app.route("/api/user/map_layers", methods=["GET"])
@jwt_required()
def get_map_layers():
    user_id = int(get_jwt_identity())
    con     = db()
    row     = con.execute("SELECT map_layers FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    if not row or not row["map_layers"]:
        return jsonify({})
    return jsonify(json.loads(row["map_layers"]))


@app.route("/api/user/map_layers", methods=["POST"])
@jwt_required()
def set_map_layers():
    user_id = int(get_jwt_identity())
    data    = request.get_json() or {}
    allowed = {"transit","roadLabels","neighborhoods","parks","konbini","supermarket","kindergarten","clinic"}
    layers  = {k: bool(v) for k, v in data.items() if k in allowed}
    con     = db()
    con.execute("""
        INSERT INTO user_settings (user_id, map_layers)
        VALUES (:uid, :ml)
        ON CONFLICT(user_id) DO UPDATE SET map_layers=excluded.map_layers, updated_at=datetime('now')
    """, {"uid": user_id, "ml": json.dumps(layers)})
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── User profile (lang, commute destinations, AI demographics) ────────────────

@app.route("/api/user/profile", methods=["GET"])
@jwt_required()
def get_user_profile():
    user_id = int(get_jwt_identity())
    con     = db()
    row     = con.execute(
        "SELECT lang, commute_dests, ai_profile, personal_info FROM user_settings WHERE user_id=?", (user_id,)
    ).fetchone()
    user    = con.execute("SELECT google_id FROM users WHERE id=?", (user_id,)).fetchone()
    con.close()
    if not row:
        return jsonify({"lang": "ja", "commute_dests": [], "ai_profile": {}, "personal_info": {}, "is_google_user": False})
    return jsonify({
        "lang":          row["lang"] or "ja",
        "commute_dests": json.loads(row["commute_dests"] or "[]"),
        "ai_profile":    json.loads(row["ai_profile"]    or "{}"),
        "personal_info": json.loads(row["personal_info"] or "{}"),
        "is_google_user": bool(user and user["google_id"]),
    })


@app.route("/api/user/profile", methods=["POST"])
@jwt_required()
def set_user_profile():
    user_id = int(get_jwt_identity())
    data    = request.get_json() or {}
    con     = db()

    updates = {}
    if "lang" in data:
        updates["lang"] = data["lang"] if data["lang"] in ("ja", "en") else "ja"
    if "commute_dests" in data:
        dests = [str(d)[:200] for d in (data["commute_dests"] or []) if str(d).strip()]
        updates["commute_dests"] = json.dumps(dests[:10])
    if "ai_profile" in data and isinstance(data["ai_profile"], dict):
        allowed_keys = {"household", "age_range", "income_bracket", "notes"}
        profile = {k: v for k, v in data["ai_profile"].items() if k in allowed_keys}
        updates["ai_profile"] = json.dumps(profile)
    if "personal_info" in data and isinstance(data["personal_info"], dict):
        allowed = {"purpose","household","age_range","income_bracket","budget_min","budget_max",
                   "preferred_wards","preferred_layouts","move_in_timing","pets","smoking",
                   "notes","onboarding_done","display_name","num_children"}
        info = {k: v for k, v in data["personal_info"].items() if k in allowed}
        updates["personal_info"] = json.dumps(info)
        # sync display_name to users table if provided
        if "display_name" in info and str(info["display_name"]).strip():
            user_id = int(get_jwt_identity())
            con.execute("UPDATE users SET display_name=? WHERE id=?",
                        (str(info["display_name"]).strip(), user_id))

    if not updates:
        con.close()
        return jsonify({"status": "ok"})

    set_clause = ", ".join(f"{k}=:{k}" for k in updates)
    updates["uid"] = user_id
    con.execute(f"""
        INSERT INTO user_settings (user_id, {", ".join(k for k in updates if k != "uid")})
        VALUES (:uid, {", ".join(f":{k}" for k in updates if k != "uid")})
        ON CONFLICT(user_id) DO UPDATE SET {set_clause}, updated_at=datetime('now')
    """, updates)
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── Saved listings ────────────────────────────────────────────────────────────

@app.route("/api/user/saved", methods=["GET"])
@jwt_required()
def get_saved_listings():
    user_id = int(get_jwt_identity())
    con     = db()
    rows    = con.execute(
        "SELECT listing_id, saved_at FROM user_saved_listings WHERE user_id=? ORDER BY saved_at DESC",
        (user_id,)
    ).fetchall()
    con.close()
    return jsonify({"saved": [dict(r) for r in rows]})


@app.route("/api/user/saved/<listing_id>", methods=["POST"])
@jwt_required()
def save_listing(listing_id):
    user_id = int(get_jwt_identity())
    con     = db()
    con.execute(
        "INSERT OR IGNORE INTO user_saved_listings (user_id, listing_id) VALUES (?,?)",
        (user_id, listing_id)
    )
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


@app.route("/api/user/saved/<listing_id>", methods=["DELETE"])
@jwt_required()
def unsave_listing(listing_id):
    user_id = int(get_jwt_identity())
    con     = db()
    con.execute(
        "DELETE FROM user_saved_listings WHERE user_id=? AND listing_id=?",
        (user_id, listing_id)
    )
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── Listing view history ──────────────────────────────────────────────────────

@app.route("/api/user/view/<listing_id>", methods=["POST"])
@jwt_required()
def record_view(listing_id):
    user_id = int(get_jwt_identity())
    con     = db()
    con.execute("""
        INSERT INTO user_listing_views (user_id, listing_id, last_viewed, view_count)
        VALUES (?, ?, datetime('now'), 1)
        ON CONFLICT(user_id, listing_id) DO UPDATE SET
            last_viewed=datetime('now'), view_count=view_count+1
    """, (user_id, listing_id))
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── Listing facilities ────────────────────────────────────────────────────────

@app.route("/api/images/<int:image_id>")
def serve_listing_image(image_id):
    con = db()
    row = con.execute("SELECT local_path FROM listing_images WHERE id=?", (image_id,)).fetchone()
    con.close()
    if not row or not row["local_path"] or not os.path.isfile(row["local_path"]):
        return "", 404
    return send_file(row["local_path"])


@app.route("/api/listings/<listing_id>/images")
def get_listing_images(listing_id):
    con = db()
    rows = con.execute(
        "SELECT id, image_type FROM listing_images "
        "WHERE listing_id=? AND local_path IS NOT NULL "
        "ORDER BY CASE image_type WHEN 'floor_plan' THEN 0 WHEN 'exterior' THEN 1 ELSE 2 END, id",
        (listing_id,),
    ).fetchall()
    con.close()
    return jsonify([{"url": f"/api/images/{r['id']}", "type": r["image_type"]} for r in rows])


@app.route("/api/listings/<listing_id>/floor_plan_analysis")
def get_floor_plan_analysis(listing_id):
    con = db()
    row = con.execute(
        "SELECT floor_plan_data FROM listings WHERE id=?", (listing_id,)
    ).fetchone()
    con.close()
    if not row or not row["floor_plan_data"]:
        return jsonify({"error": "no analysis available"}), 404
    import json as _json
    return jsonify(_json.loads(row["floor_plan_data"]))


@app.route("/api/listings/<listing_id>/facilities")
def get_listing_facilities(listing_id):
    con  = db()
    rows = con.execute(
        "SELECT type, name, lat, lng, distance_m FROM listing_facilities "
        " WHERE listing_id=? ORDER BY distance_m",
        (listing_id,),
    ).fetchall()
    con.close()
    return jsonify({"facilities": [dict(r) for r in rows]})


# ── User notifications ────────────────────────────────────────────────────────

@app.route("/api/user/notifications", methods=["GET"])
@jwt_required()
def get_user_notifications():
    user_id = int(get_jwt_identity())
    con     = db()
    rows    = con.execute(
        "SELECT * FROM user_notifications WHERE user_id=? ORDER BY id", (user_id,)
    ).fetchall()
    con.close()
    return jsonify({"notifications": [dict(r) for r in rows]})


@app.route("/api/user/notifications", methods=["POST"])
@jwt_required()
def add_user_notification():
    user_id = int(get_jwt_identity())
    data    = request.get_json() or {}
    ntype   = data.get("type", "").lower()
    target  = data.get("target", "").strip()
    label   = data.get("label", "").strip()

    if ntype not in ("slack", "line", "email"):
        return jsonify({"error": "type は slack / line / email のいずれかです"}), 400
    if not target:
        return jsonify({"error": "送信先を入力してください"}), 400

    con = db()
    con.execute(
        "INSERT INTO user_notifications (user_id, type, target, label) VALUES (?,?,?,?)",
        (user_id, ntype, target, label or ntype)
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM user_notifications WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()
    con.close()
    return jsonify(dict(row)), 201


@app.route("/api/user/notifications/<int:notif_id>", methods=["DELETE"])
@jwt_required()
def delete_user_notification(notif_id):
    user_id = int(get_jwt_identity())
    con     = db()
    con.execute("DELETE FROM user_notifications WHERE id=? AND user_id=?", (notif_id, user_id))
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


@app.route("/api/user/notifications/<int:notif_id>/toggle", methods=["POST"])
@jwt_required()
def toggle_user_notification(notif_id):
    user_id = int(get_jwt_identity())
    con     = db()
    con.execute(
        "UPDATE user_notifications SET enabled=1-enabled WHERE id=? AND user_id=?",
        (notif_id, user_id)
    )
    con.commit()
    con.close()
    return jsonify({"status": "ok"})


# ── OAuth connect helpers ─────────────────────────────────────────────────────

def _store_oauth_state(con, key, user_id):
    con.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)",
                (key, str(user_id)))
    con.commit()

def _pop_oauth_state(con, key):
    row = con.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
    if row:
        con.execute("DELETE FROM app_config WHERE key=?", (key,))
        con.commit()
        return int(row["value"])
    return None

def _connected_page(type_name):
    return f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8">
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#f6f5f0}}.box{{background:#fff;border-radius:14px;padding:2rem 2.5rem;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.1)}}.ok{{font-size:48px}}.msg{{font-size:18px;font-weight:600;margin:.5rem 0}}.sub{{font-size:13px;color:#888}}</style>
</head><body><div class="box">
<div class="ok">✅</div>
<div class="msg">{type_name} を連携しました</div>
<div class="sub">このタブを閉じてください</div>
</div></body></html>"""

def _error_page(msg):
    return f"<p style='font-family:system-ui;padding:2rem;color:red'>エラー: {msg}</p>", 400


@app.route("/api/auth/line/start")
@jwt_required()
def line_oauth_start():
    if not LINE_CLIENT_ID:
        return jsonify({"error": "LINE_NOTIFY_CLIENT_ID が設定されていません"}), 503
    user_id = int(get_jwt_identity())
    state   = secrets.token_hex(16)
    con     = db()
    _store_oauth_state(con, f"line_state_{state}", user_id)
    con.close()
    url = "https://notify-bot.line.me/oauth/authorize?" + urlencode({
        "response_type": "code",
        "client_id":     LINE_CLIENT_ID,
        "redirect_uri":  LINE_REDIRECT_URI,
        "scope":         "notify",
        "state":         state,
    })
    return jsonify({"url": url})


@app.route("/api/auth/line/callback")
def line_oauth_callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _error_page("code または state がありません")
    con     = db()
    user_id = _pop_oauth_state(con, f"line_state_{state}")
    if not user_id:
        con.close()
        return _error_page("無効な state です（期限切れの可能性があります）")
    try:
        r = requests.post("https://notify-api.line.me/oauth/token", data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  LINE_REDIRECT_URI,
            "client_id":     LINE_CLIENT_ID,
            "client_secret": LINE_CLIENT_SECRET,
        }, timeout=10)
        token = r.json().get("access_token")
        if not token:
            con.close()
            return _error_page("トークン取得失敗")
        con.execute(
            "INSERT INTO user_notifications (user_id, type, target, label) VALUES (?,?,?,?)",
            (user_id, "line", token, "LINE Notify")
        )
        con.commit()
    except Exception as e:
        con.close()
        return _error_page(str(e))
    con.close()
    return _connected_page("LINE Notify")


@app.route("/api/auth/slack/start")
@jwt_required()
def slack_oauth_start():
    if not SLACK_CLIENT_ID:
        return jsonify({"error": "SLACK_CLIENT_ID が設定されていません"}), 503
    user_id = int(get_jwt_identity())
    state   = secrets.token_hex(16)
    con     = db()
    _store_oauth_state(con, f"slack_state_{state}", user_id)
    con.close()
    url = "https://slack.com/oauth/v2/authorize?" + urlencode({
        "client_id":   SLACK_CLIENT_ID,
        "scope":       "incoming-webhook",
        "redirect_uri": SLACK_REDIRECT_URI,
        "state":       state,
    })
    return jsonify({"url": url})


@app.route("/api/auth/slack/callback")
def slack_oauth_callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _error_page("code または state がありません")
    con     = db()
    user_id = _pop_oauth_state(con, f"slack_state_{state}")
    if not user_id:
        con.close()
        return _error_page("無効な state です")
    try:
        r = requests.post("https://slack.com/api/oauth.v2.access", data={
            "client_id":     SLACK_CLIENT_ID,
            "client_secret": SLACK_CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  SLACK_REDIRECT_URI,
        }, timeout=10)
        d           = r.json()
        webhook_url = d.get("incoming_webhook", {}).get("url")
        channel     = d.get("incoming_webhook", {}).get("channel", "")
        if not webhook_url:
            con.close()
            return _error_page("Webhook URL 取得失敗: " + str(d.get("error", "")))
        con.execute(
            "INSERT INTO user_notifications (user_id, type, target, label) VALUES (?,?,?,?)",
            (user_id, "slack", webhook_url, f"Slack {channel}")
        )
        con.commit()
    except Exception as e:
        con.close()
        return _error_page(str(e))
    con.close()
    return _connected_page("Slack")


@app.route("/api/auth/google/start")
def google_oauth_start():
    if not GOOGLE_CLIENT_ID:
        return _error_page("GOOGLE_CLIENT_ID が設定されていません")
    state = secrets.token_hex(16)
    con   = db()
    _store_oauth_state(con, f"google_state_{state}", 0)
    con.close()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    })
    return redirect(url)


@app.route("/api/auth/google/callback")
def google_oauth_callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _error_page("OAuth パラメータが不正です")
    con = db()
    stored = con.execute("SELECT value FROM app_config WHERE key=?",
                         (f"google_state_{state}",)).fetchone()
    if not stored:
        con.close()
        return _error_page("セッションが無効です（再度お試しください）")
    con.execute("DELETE FROM app_config WHERE key=?", (f"google_state_{state}",))
    con.commit()
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        }, timeout=10)
        tokens       = r.json()
        access_token = tokens.get("access_token")
        if not access_token:
            con.close()
            return _error_page("トークン取得に失敗しました")
        ui       = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        userinfo     = ui.json()
        google_id    = userinfo.get("id", "")
        email        = userinfo.get("email", "").strip().lower()
        display_name = userinfo.get("name", "")
        avatar_url   = userinfo.get("picture", "")
        if not google_id or not email:
            con.close()
            return _error_page("ユーザー情報の取得に失敗しました")

        user = con.execute("SELECT * FROM users WHERE google_id=?", (google_id,)).fetchone()
        if not user:
            user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user:
                con.execute("UPDATE users SET google_id=?, avatar_url=? WHERE id=?", (google_id, avatar_url, user["id"]))
                con.commit()
            else:
                con.execute(
                    "INSERT INTO users (email, password_hash, display_name, google_id, avatar_url) VALUES (?,?,?,?,?)",
                    (email, "", display_name or email.split("@")[0], google_id, avatar_url)
                )
                con.commit()
                user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                con.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user["id"],))
                con.commit()
        # Always refresh avatar from Google
        if avatar_url:
            con.execute("UPDATE users SET avatar_url=? WHERE id=?", (avatar_url, user["id"]))
            con.commit()

        pinfo_row = con.execute(
            "SELECT personal_info FROM user_settings WHERE user_id=?", (user["id"],)
        ).fetchone()
        pinfo = json.loads(pinfo_row["personal_info"] or "{}") if pinfo_row else {}
        needs_onboarding = not pinfo.get("onboarding_done", False)

        token = create_access_token(identity=str(user["id"]))
        con.close()
    except Exception as e:
        try: con.close()
        except Exception: pass
        return _error_page(f"認証エラー: {e}")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0d1117;color:#e6edf3}}.box{{text-align:center}}</style>
</head><body><div class="box">
<div style="font-size:48px">✅</div>
<p style="font-size:16px;font-weight:600;margin:8px 0">ログインしました</p>
<p style="font-size:13px;color:#888">このタブを閉じています…</p>
</div><script>
try{{window.opener&&window.opener.postMessage({{
  type:'google-auth',
  token:{json.dumps(token)},
  email:{json.dumps(email)},
  display_name:{json.dumps(display_name or str(user["display_name"] or ""))},
  avatar_url:{json.dumps(avatar_url or str(user.get("avatar_url") or ""))},
  needs_onboarding:{'true' if needs_onboarding else 'false'}
}},'*');}}catch(e){{}}
setTimeout(()=>window.close(),600);
</script></body></html>"""


@app.route("/api/auth/github/start")
def github_oauth_start():
    if not GITHUB_CLIENT_ID:
        return _error_page("GITHUB_CLIENT_ID が設定されていません")
    state = secrets.token_hex(16)
    con   = db()
    _store_oauth_state(con, f"github_state_{state}", 0)
    con.close()
    url = "https://github.com/login/oauth/authorize?" + urlencode({
        "client_id":    GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope":        "user:email",
        "state":        state,
    })
    return redirect(url)


@app.route("/api/auth/github/callback")
def github_oauth_callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _error_page("OAuth パラメータが不正です")
    con = db()
    stored = con.execute("SELECT value FROM app_config WHERE key=?",
                         (f"github_state_{state}",)).fetchone()
    if not stored:
        con.close()
        return _error_page("セッションが無効です（再度お試しください）")
    con.execute("DELETE FROM app_config WHERE key=?", (f"github_state_{state}",))
    con.commit()
    try:
        r = requests.post("https://github.com/login/oauth/access_token", data={
            "client_id":     GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code":          code,
            "redirect_uri":  GITHUB_REDIRECT_URI,
        }, headers={"Accept": "application/json"}, timeout=10)
        tokens       = r.json()
        access_token = tokens.get("access_token")
        if not access_token:
            con.close()
            return _error_page("トークン取得に失敗しました: " + tokens.get("error_description", ""))

        ui       = requests.get("https://api.github.com/user",
                                headers={"Authorization": f"Bearer {access_token}",
                                         "Accept": "application/vnd.github+json"}, timeout=10)
        userinfo    = ui.json()
        github_id    = str(userinfo.get("id", ""))
        display_name = userinfo.get("name") or userinfo.get("login", "")
        avatar_url   = userinfo.get("avatar_url", "")

        # GitHub may not expose email; fetch from /user/emails if needed
        email = userinfo.get("email", "") or ""
        if not email:
            er = requests.get("https://api.github.com/user/emails",
                              headers={"Authorization": f"Bearer {access_token}",
                                       "Accept": "application/vnd.github+json"}, timeout=10)
            for e in er.json():
                if e.get("primary") and e.get("verified"):
                    email = e["email"].lower()
                    break
            if not email and er.json():
                email = er.json()[0].get("email", "").lower()

        if not github_id:
            con.close()
            return _error_page("ユーザー情報の取得に失敗しました")
        if not email:
            email = f"github_{github_id}@noemail.local"

        user = con.execute("SELECT * FROM users WHERE github_id=?", (github_id,)).fetchone()
        if not user:
            user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user:
                con.execute("UPDATE users SET github_id=?, avatar_url=? WHERE id=?", (github_id, avatar_url, user["id"]))
                con.commit()
            else:
                con.execute(
                    "INSERT INTO users (email, password_hash, display_name, github_id, avatar_url) VALUES (?,?,?,?,?)",
                    (email, "", display_name or email.split("@")[0], github_id, avatar_url)
                )
                con.commit()
                user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                con.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user["id"],))
                con.commit()
        # Always refresh avatar from GitHub
        if avatar_url:
            con.execute("UPDATE users SET avatar_url=? WHERE id=?", (avatar_url, user["id"]))
            con.commit()

        pinfo_row = con.execute(
            "SELECT personal_info FROM user_settings WHERE user_id=?", (user["id"],)
        ).fetchone()
        pinfo = json.loads(pinfo_row["personal_info"] or "{}") if pinfo_row else {}
        needs_onboarding = not pinfo.get("onboarding_done", False)

        token = create_access_token(identity=str(user["id"]))
        con.close()
    except Exception as e:
        try: con.close()
        except Exception: pass
        return _error_page(f"認証エラー: {e}")

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0d1117;color:#e6edf3}}.box{{text-align:center}}</style>
</head><body><div class="box">
<div style="font-size:48px">✅</div>
<p style="font-size:16px;font-weight:600;margin:8px 0">ログインしました</p>
<p style="font-size:13px;color:#888">このタブを閉じています…</p>
</div><script>
try{{window.opener&&window.opener.postMessage({{
  type:'github-auth',
  token:{json.dumps(token)},
  email:{json.dumps(email)},
  display_name:{json.dumps(display_name or str(user["display_name"] or ""))},
  avatar_url:{json.dumps(avatar_url or str(user.get("avatar_url") or ""))},
  needs_onboarding:{'true' if needs_onboarding else 'false'}
}},'*');}}catch(e){{}}
setTimeout(()=>window.close(),600);
</script></body></html>"""


@app.route("/api/auth/telegram/start")
@jwt_required()
def telegram_start():
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "TELEGRAM_BOT_TOKEN が設定されていません"}), 503
    user_id = int(get_jwt_identity())
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=10)
        d = r.json()
        if not d.get("ok"):
            return jsonify({"error": "Bot token 無効: " + d.get("description", "")}), 503
        bot_username = d["result"]["username"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    code = secrets.token_hex(16)
    con  = db()
    _store_oauth_state(con, f"tg_state_{code}", user_id)
    con.close()
    return jsonify({"url": f"https://t.me/{bot_username}?start={code}", "code": code})


@app.route("/api/auth/telegram/poll")
@jwt_required()
def telegram_poll():
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"done": False})
    user_id = int(get_jwt_identity())
    code    = request.args.get("code", "")
    if not code:
        return jsonify({"done": False})
    with _tg_poll_lock:
        con = db()
        try:
            row    = con.execute("SELECT value FROM app_config WHERE key='tg_offset'").fetchone()
            offset = int(row[0]) + 1 if row else 0
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": offset, "timeout": 0, "limit": 100},
                timeout=15,
            )
            d = r.json()
            if not d.get("ok"):
                con.close()
                return jsonify({"done": False})
            updates    = d.get("result", [])
            done       = False
            new_offset = offset - 1
            for upd in updates:
                new_offset = max(new_offset, upd["update_id"])
                msg      = upd.get("message", {})
                text     = msg.get("text", "")
                chat_id  = str(msg.get("chat", {}).get("id", ""))
                if not text.startswith("/start"):
                    continue
                parts    = text.split(None, 1)
                upd_code = parts[1].strip() if len(parts) > 1 else ""
                if not upd_code:
                    continue
                state_row = con.execute(
                    "SELECT value FROM app_config WHERE key=?", (f"tg_state_{upd_code}",)
                ).fetchone()
                if not state_row:
                    continue
                uid = int(state_row[0])
                con.execute("DELETE FROM app_config WHERE key=?", (f"tg_state_{upd_code}",))
                con.execute(
                    "INSERT OR IGNORE INTO user_notifications (user_id, type, target, label) VALUES (?,?,?,?)",
                    (uid, "telegram", f"{TELEGRAM_BOT_TOKEN}|{chat_id}", f"Telegram ({chat_id})")
                )
                con.commit()
                if uid == user_id and upd_code == code:
                    done = True
            if updates:
                con.execute(
                    "INSERT OR REPLACE INTO app_config (key, value) VALUES ('tg_offset', ?)",
                    (str(new_offset),)
                )
                con.commit()
            con.close()
            return jsonify({"done": done})
        except Exception as e:
            con.close()
            return jsonify({"done": False, "error": str(e)})


# ── Legacy config (scraper still reads this for its own filter) ───────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


@app.route("/api/meta")
def meta():
    return jsonify({"wards": TOKYO_WARDS, "layouts": LAYOUTS})


@app.route("/api/log")
def get_log():
    lines = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            lines = f.readlines()[-50:]
    return jsonify({"lines": [l.rstrip() for l in lines]})


# ── Listings ──────────────────────────────────────────────────────────────────

@app.route("/api/listings")
def get_listings():
    if not os.path.exists(DB_FILE):
        return jsonify({"listings": [], "geocoded_now": 0})

    max_age_min = request.args.get("max_age_min", default=90, type=int)
    cutoff      = (datetime.now() - timedelta(minutes=max_age_min)).isoformat()

    con = db()
    cur = con.execute(
        "SELECT id,name,ward,layout,rent,size_m2,url,first_seen,last_seen,"
        "       COALESCE(source,'jkk') AS source,"
        "       COALESCE(address, ward) AS address, lat, lng, geocoded_at,"
        "       disappeared_at, walk_min, walk_m, nearest_station, thumbnail_url "
        "  FROM listings "
        " WHERE last_seen >= ? "
        " ORDER BY last_seen DESC",
        (cutoff,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    placed          = [r for r in rows if r["lat"] is not None and r["lng"] is not None]
    geo_pending     = sum(1 for r in rows if r["lat"] is None and r["geocoded_at"] is None)
    station_pending = sum(1 for r in rows if r["lat"] is not None and not r["nearest_station"])
    pending         = geo_pending + station_pending

    if pending:
        _ensure_geocoding()

    return jsonify({
        "listings":        placed,
        "total":           len(rows),
        "placed":          len(placed),
        "pending":         pending,
        "geo_pending":     geo_pending,
        "station_pending": station_pending,
        "max_age_min":     max_age_min,
    })


@app.route("/api/disappeared")
def get_disappeared():
    if not os.path.exists(DB_FILE):
        return jsonify({"listings": []})
    limit = request.args.get("limit", default=30, type=int)
    con   = db()
    cur   = con.execute(
        "SELECT id,name,ward,layout,rent,size_m2,url,first_seen,last_seen,"
        "       disappeared_at, walk_min, walk_m, nearest_station, COALESCE(source,'jkk') AS source,"
        "       COALESCE(address, ward) AS address "
        "  FROM listings "
        " WHERE disappeared_at IS NOT NULL "
        " ORDER BY disappeared_at DESC "
        " LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return jsonify({"listings": rows})


@app.route("/api/route")
def proxy_directions():
    origin      = request.args.get("origin", "").strip()
    destination = request.args.get("destination", "").strip()
    mode        = request.args.get("mode", "transit")
    dep         = request.args.get("departure_time", "now")

    if not origin or not destination:
        return jsonify({"status": "INVALID_REQUEST"}), 400
    if not GOOGLE_MAPS_SERVER_KEY:
        return jsonify({"status": "REQUEST_DENIED", "error_message": "GOOGLE_MAPS_SERVER_KEY not set"}), 503

    params = {
        "origin":           origin,
        "destination":      destination,
        "mode":             mode,
        "departure_time":   dep,
        "language":         "ja",
        "region":           "JP",
        "key":              GOOGLE_MAPS_SERVER_KEY,
    }
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params=params, timeout=10
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "UNKNOWN_ERROR", "error_message": str(e)}), 502


# ── Claude chat ────────────────────────────────────────────────────────────────

CHAT_SYSTEM = """You are a helpful real estate assistant for a Tokyo/Kanagawa housing monitor that tracks JKK (東京都公社住宅) and UR (都市再生機構) rental vacancies.
Today's date: {date}
Always respond in the same language the user writes in (Japanese or English).
Use tools to fetch real live data — never guess or invent listing IDs, names, or details.
{open_listing_context}
Rules:
- Always call search_listings first to find properties and obtain valid listing IDs. Never call get_listing_details with a guessed or remembered ID — only use IDs returned by search_listings in the same conversation.
- Exception: if a property card is currently open (shown above as "Currently open property card"), you already know its listing_id — use it directly for get_commute_time or get_listing_details without calling search_listings first.
- When you call search_listings, the UI automatically renders clickable property cards below your message. Do not mention photos, thumbnails, or image availability — just describe the listings in text and let the cards do the rest.
- Use get_commute_time for ANY question about travel time, commute, or how long to reach a place. Never estimate — always call the tool. For transit mode, when the tool returns status "calculating", tell the user the route is being calculated and will appear below — do not guess the time.
- Use center_map whenever the user asks to see a location or property on the map ("show me", "where is", "center on", "zoom to"). You can call it with a listing_id (coordinates are looked up automatically) or with lat/lng.
- Use select_listing only for ONE specific named property ("open that one", "select it"). Never call it in a loop.
- Use set_map_filter when the user wants to show/hide multiple listings by criteria ("show listings above 50m²", "filter to 1LDK", "only JKK"). This updates the map markers and table instantly.
- Do not use emojis. Keep responses concise and factual.
- Use find_nearby_place when the user asks about any facility near a property (hospital, school, gym, etc.). Always call the tool — never skip it, never redirect to Google Maps, never guess. If the tool returns an error, report the exact error message and stop — do NOT follow up with guesses or suggestions about what might be nearby. If results come back, immediately call get_commute_time with the nearest place name as destination to give an actual travel time. For Japanese facility types that are poorly indexed by English place_type, also pass a Japanese keyword: kindergarten→keyword='幼稚園', nursery/daycare→keyword='保育園', elementary school→keyword='小学校', junior high→keyword='中学校', high school→keyword='高校', supermarket→keyword='スーパー', pharmacy→keyword='薬局', clinic→keyword='クリニック'.
- If a tool returns no results, say so plainly and offer to try different search parameters.
- Use show_listing_images whenever the user asks to see photos, pictures, images, or the floor plan for a property. If a property card is open, use its listing_id directly. Images render inline in the chat — no need to describe them in text.
- HARD RULE: NEVER say images are "rendering", "displaying", "showing", "loading", or "appearing" in text. NEVER narrate what the tool is doing. Call show_listing_images — that IS the display. If you describe images in text without calling the tool, the user sees nothing.
- Use batch_commute_filter when the user asks to filter by commute time to a destination. IMPORTANT: if the query is about the user's saved listings ("which of my saved...", "remove saved listings that are...", etc.), pass their listing IDs as listing_ids — this is MUCH faster and avoids timeouts. Only omit listing_ids when the user wants to search ALL properties. After it returns, immediately call save_listings or remove_listings with the matching IDs if the user asked to save/remove them.
- Use save_listings to add listings to the user's saved selections. Call it immediately after batch_commute_filter or search_listings when the user explicitly asks to save/bookmark the results. To add all listings with a floor plan: call search_listings(has_floor_plan=true, limit=200) then save_listings with all returned IDs. To find listings with any photo: use has_images=true.
- To search all 23 Tokyo special wards (23区, 23-ku, 都内, inner Tokyo, etc.): call search_listings with wards=["23区"] — the server expands this automatically. Never claim there are no results without calling the tool first.
- Use remove_listings to remove listings from saved selections. The user's current saved list with photo counts is provided in context — use the listing IDs directly. For "remove listings without photos" filter to those with photos=0.
- HARD RULE: Never end a response with follow-up suggestions, offers to help further, or questions ("Would you like more details?", "Would you like help with anything else?", "Is there anything else I can help you with?" etc.). Answer exactly what was asked, then stop. No trailing questions or offers.
- HARD RULE: Never ask for confirmation before using a tool you already have. If the user asks you to save, remove, filter, or show something and you have the tool for it, do it immediately and say done. Do not ask "Shall I go ahead?", "Would you like me to?", "Do you want me to?" — just act.
- HARD RULE: NEVER rely on chat history to claim an action is complete. Previous turns are stale — the user may have changed state since then. Every action request MUST re-execute the full tool chain from scratch in the current turn. If you see a prior assistant message claiming "I added X listings," ignore it — call the tools again now.
- HARD RULE: NEVER answer count or stats questions ("how many", "how many listings", "how many floor plans", "how many saved", etc.) from memory or prior messages. Database counts change constantly. Always call search_listings (or the appropriate tool) to get a live count, even if you answered the same question moments ago.
- If the user asks a property-specific question (photos, floor plan, commute, nearby facilities) and there is NO open property card: search for candidates using any name/ward/layout mentioned, then show the cards. If exactly one result comes back, proceed immediately (call show_listing_images, get_commute_time, etc.) using that listing_id. If multiple results come back, tell the user "Click the one you mean and I'll show the floor plan" (or whatever was requested). If there is truly nothing to search on, reply: "Please open a property card first — click any listing on the map or in the table, then ask again."
- When showing disambiguation candidates (multiple search results for a property-specific request), always call pick_listing(pending_message='<original action>') immediately after search_listings — e.g. pick_listing(pending_message='show floor plan'). Do NOT call pick_listing if only one result was found (proceed directly instead).
- For queries using relative size language ("大きいリビング", "large kitchen", "spacious", "広いLDK", "huge living room", etc.) WITHOUT a superlative ("the biggest", "which one"): call get_room_stats FIRST, then use p75 for "large/spacious" and p90 for "huge/very large" as the min_living_area_m2 filter. Never hardcode a threshold. If get_room_stats returns no data, search without the filter.
- HARD RULE: Any answer that resolves to a specific property or a small set (≤ ~5 listings) MUST render clickable cards — never answer with property names in plain text alone. How to render:
  • Singular superlatives ("the biggest living room", "which has the most", "the cheapest", "the largest apartment", etc.): call search_listings with the matching sort_by + limit=1. Do NOT call get_room_stats or add a min_ filter — sort_by alone finds the winner. One card must appear.
  • Plural top-N ("top 3 biggest", "a few options with large living rooms"): sort_by + limit=N.
  • Multi-step queries where the winner is found via intermediate tools (facility counts, custom ranking, floor plan comparison, etc.): call show_listings(listing_ids=[…]) with the identified IDs at the end.
  • Single known property: call show_listings(listing_ids=[id]).
  The rule: if you can name it, you must show it. Text-only answers for identifiable properties are never acceptable.
  Do NOT call show_listings for operational context: commute calculation in progress, save/remove confirmation, image display.
- Use run_sql for any question not expressible with existing tools: facility counts, rent history trends, cross-table filters, floor plan JSON comparisons, etc. Write a single SELECT; the schema is in the tool description. Always filter disappeared_at IS NULL for active listings. If the SELECT includes an 'id' column from listings, cards render automatically.
- Floor plan analysis (from get_room_stats and get_listing_details floor_plan_rooms): living_area_m2 is the combined LDK/LD/L area. kitchen_open=true means the kitchen opens to the living area with no separating door. Use these fields to answer questions about room layout and to filter searches.
"""

_CHAT_TOOLS = [
    {
        "name": "search_listings",
        "description": "Search currently available rental properties. Returns up to 20 matches with id, name, ward, rent, layout, size_m2, url, source, nearest_station, walk_min, thumbnail_url. Use has_floor_plan=true for listings with a floor plan; use has_images=true for listings with any downloaded photo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":          {"type": "string",  "description": "Partial property name to search (LIKE match), e.g. 'サンヴァリエ'"},
                "ward":          {"type": "string",  "description": "Single ward/city, e.g. 新宿区. For multiple wards use 'wards' array instead."},
                "wards":         {"type": "array", "items": {"type": "string"}, "description": "List of wards/cities. Use '23区' as a single element to match all 23 Tokyo special wards at once."},
                "layout":        {"type": "string",  "description": "Floor plan exact, e.g. 1LDK, 2DK"},
                "min_rent":      {"type": "integer", "description": "Min monthly rent JPY"},
                "max_rent":      {"type": "integer", "description": "Max monthly rent JPY"},
                "min_size":      {"type": "number",  "description": "Min floor area m²"},
                "max_size":      {"type": "number",  "description": "Max floor area m²"},
                "source":        {"type": "string",  "enum": ["jkk","ur"], "description": "jkk=公社 ur=UR"},
                "max_walk":      {"type": "integer", "description": "Max walk min to nearest station"},
                "has_floor_plan":       {"type": "boolean", "description": "If true, only return listings that have a floor plan image"},
                "has_images":          {"type": "boolean", "description": "If true, only return listings that have at least one downloaded photo"},
                "min_living_area_m2":  {"type": "number",  "description": "Min living/dining/kitchen (LDK/LD/L) area in m² from floor plan analysis. Requires floor plan data — call get_room_stats first to pick an evidence-based threshold."},
                "sort_by":             {"type": "string",  "enum": ["rent_asc","rent_desc","living_area_desc","size_desc"], "description": "Sort order. Default rent_asc. Use living_area_desc for 'biggest living room', size_desc for 'largest apartment'."},
                "limit":               {"type": "integer", "description": "Max results, default 10, use 200 to get all"},
            },
        },
    },
    {
        "name": "show_listings",
        "description": "Render one or more property listing cards for the user to click on. Call this when your response has identified specific properties through multi-step reasoning, facility comparisons, floor plan analysis, or any path other than search_listings. Also use for a single known property instead of select_listing.",
        "input_schema": {
            "type": "object",
            "required": ["listing_ids"],
            "properties": {
                "listing_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs of the listings to render as clickable cards"},
            },
        },
    },
    {
        "name": "get_statistics",
        "description": "Summary stats: total available, counts by source/ward, rent range, layout breakdown.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_room_stats",
        "description": "Distribution statistics for living-area size (m²) and other room features (toilet count, open kitchen %) extracted from floor plan analysis. Call this BEFORE searching for 'large', 'huge', 'spacious', 'wide' living rooms — use the p75 value as min_living_area_m2 for 'large' and p90 for 'huge/very large'. Never guess a size threshold from training data.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_listing_details",
        "description": "Full details for one listing including nearby facilities and floor_plan_ocr (individual room dimensions extracted from floor plan images via OCR, if available).",
        "input_schema": {
            "type": "object",
            "required": ["listing_id"],
            "properties": {"listing_id": {"type": "string"}},
        },
    },
    {
        "name": "center_map",
        "description": "Pan and zoom the map to a listing or coordinates. Use when user says 'show on map', 'center on', 'zoom to', or 'where is'. Prefer listing_id — coordinates are resolved automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "listing_id": {"type": "string",  "description": "Listing ID whose coordinates to center on"},
                "lat":        {"type": "number",  "description": "Latitude (use only when no listing_id)"},
                "lng":        {"type": "number",  "description": "Longitude (use only when no listing_id)"},
                "zoom":       {"type": "integer", "description": "Zoom level 10-18, default 15"},
            },
        },
    },
    {
        "name": "select_listing",
        "description": "Select and highlight ONE specific listing on the map and open its detail card. Use only for a single named property. For 'show all X' or multi-listing filtering, use set_map_filter instead.",
        "input_schema": {
            "type": "object",
            "required": ["listing_id"],
            "properties": {"listing_id": {"type": "string"}},
        },
    },
    {
        "name": "get_commute_time",
        "description": "Get actual transit commute time from a listing to a destination using Google Maps Directions API. Use this for ANY question about travel time, commute, or 'how long to get to X'. Never estimate — always call this tool.",
        "input_schema": {
            "type": "object",
            "required": ["listing_id", "destination"],
            "properties": {
                "listing_id":  {"type": "string", "description": "Listing ID to commute from"},
                "destination": {"type": "string", "description": "Destination station or address in Japanese, e.g. '目黒駅', '新宿駅'"},
                "mode":        {"type": "string", "enum": ["transit","walking","driving","bicycling"], "description": "Travel mode, default transit"},
            },
        },
    },
    {
        "name": "set_map_filter",
        "description": "Apply filters to show/hide markers on the map. Use when the user says 'show only', 'filter to', 'select all X', 'listings above/below Y', etc. All fields are optional; omit any you don't want to change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_size": {"type": "number",  "description": "Min floor area m² (0 = clear)"},
                "max_size": {"type": "number",  "description": "Max floor area m² (0 = no limit)"},
                "min_rent": {"type": "integer", "description": "Min monthly rent JPY (0 = clear)"},
                "max_rent": {"type": "integer", "description": "Max monthly rent JPY (0 = no limit)"},
                "max_walk": {"type": "integer", "description": "Max walk min to nearest station (0 = no limit)"},
                "source":   {"type": "string",  "enum": ["jkk","ur","both"], "description": "Filter by property source"},
            },
        },
    },
    {
        "name": "show_listing_images",
        "description": "Show photos for a specific listing. Renders an image gallery directly in the chat. Use whenever the user asks to see photos, pictures, floor plans, or images for a property. Pass image_type='floor_plan' when the user specifically asks for the floor plan only, 'exterior' for exterior shots only, 'interior' for interior shots only. Omit image_type to show all images.",
        "input_schema": {
            "type": "object",
            "required": ["listing_id"],
            "properties": {
                "listing_id": {"type": "string"},
                "image_type": {"type": "string", "enum": ["floor_plan", "exterior", "interior"], "description": "Filter to only this image type. Omit to show all."},
            },
        },
    },
    {
        "name": "batch_commute_filter",
        "description": "Check transit commute time from listings to a destination and return those within the limit. When checking the user's saved listings, pass their IDs as listing_ids to avoid checking all properties (much faster). Use when user says 'find properties within X minutes of Y' or 'which of my saved listings are more than N min from Z'. Returns matching listing IDs with commute times.",
        "input_schema": {
            "type": "object",
            "required": ["destination", "max_minutes"],
            "properties": {
                "destination": {"type": "string",  "description": "Destination station or address in Japanese, e.g. '目黒駅'"},
                "max_minutes": {"type": "integer", "description": "Maximum one-way transit commute in minutes"},
                "source":      {"type": "string",  "enum": ["jkk", "ur"], "description": "Optionally restrict to one source"},
                "listing_ids": {"type": "array", "items": {"type": "string"}, "description": "If provided, only check these specific listings instead of all geocoded listings. Use this when filtering the user's saved selections."},
            },
        },
    },
    {
        "name": "save_listings",
        "description": "Add a list of listings to the user's saved selections (bookmarks). Call this after batch_commute_filter or search_listings when the user wants to save/bookmark those results.",
        "input_schema": {
            "type": "object",
            "required": ["listing_ids"],
            "properties": {
                "listing_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs to save"},
            },
        },
    },
    {
        "name": "remove_listings",
        "description": "Remove listings from the user's saved selections. Use when the user asks to remove, delete, or deselect specific listings or listings matching a condition (e.g. 'remove properties without photos', 'remove listings in Hachioji'). To find which listings lack photos, query the saved list and check for missing images.",
        "input_schema": {
            "type": "object",
            "required": ["listing_ids"],
            "properties": {
                "listing_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs to remove from saved selections"},
            },
        },
    },
    {
        "name": "pick_listing",
        "description": "Call this immediately after search_listings returns multiple candidates for a property-specific action (floor plan, photos, commute, nearby). Signals the UI to execute pending_message automatically when the user clicks one of the shown cards.",
        "input_schema": {
            "type": "object",
            "required": ["pending_message"],
            "properties": {
                "pending_message": {"type": "string", "description": "The action to perform once user picks a card, e.g. 'show floor plan' or 'show all photos'"},
            },
        },
    },
    {
        "name": "get_market_trends",
        "description": "Historical data on how long listings stayed available before disappearing. Use for questions like 'how fast do listings go?', 'how long do UR listings in Nerima last?', 'which ward has the quickest turnover?'. Returns avg/min/max days active broken down by source and ward.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ward":   {"type": "string", "description": "Filter by ward (partial match), e.g. 練馬区"},
                "source": {"type": "string", "enum": ["jkk", "ur"], "description": "Filter by source"},
            },
        },
    },
    {
        "name": "find_nearby_place",
        "description": (
            "Find nearby places of a given type near a listing. Checks the OSM-cached facility database "
            "first (same data shown in the property card Nearby Facilities tab) — this is fast and consistent. "
            "Falls back to Google Maps Places API only if the cache has no results for that type. "
            "Returns name and distance_m. After finding the closest place, call get_commute_time "
            "to get the actual walking/cycling time using the place name as destination."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "listing_id": {"type": "string", "description": "Listing ID to search near"},
                "place_type": {"type": "string", "description": "Google Maps place type, e.g. hospital, school, kindergarten, supermarket, gym, library, police, post_office"},
                "keyword":    {"type": "string", "description": "Optional Japanese keyword to narrow results, e.g. '幼稚園' for kindergartens, '保育園' for nurseries, '小学校' for elementary schools. Use this for facility types that are poorly indexed under English place types in Japan."},
                "radius_m":   {"type": "integer", "description": "Search radius in metres, default 3000, max 10000"},
            },
            "required": ["listing_id", "place_type"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Execute a read-only SELECT query against the listings database. "
            "Use this for any question not covered by other tools — facility counts, "
            "rent history, cross-table filters, floor plan JSON fields, etc.\n\n"
            "SCHEMA:\n"
            "listings(id TEXT PK, name TEXT, ward TEXT, layout TEXT, rent INT, size_m2 REAL, "
            "url TEXT, source TEXT ['jkk'|'ur'], address TEXT, lat REAL, lng REAL, "
            "first_seen TEXT, last_seen TEXT, disappeared_at TEXT [NULL=active], "
            "walk_min INT, nearest_station TEXT, floor_plan_data JSON)\n"
            "  floor_plan_data JSON fields: living_area_m2, kitchen_open, toilet_count, "
            "bathroom_count, envelope_width_m, envelope_height_m, rooms[] (label,area_m2,has_window,is_outdoor)\n\n"
            "listing_facilities(listing_id, type TEXT, name TEXT, distance_m INT)\n"
            "  type values: konbini, pharmacy, clinic, kindergarten, supermarket, school, station, hospital, park\n\n"
            "listing_images(listing_id, image_type TEXT ['floor_plan'|'exterior'|'interior'])\n\n"
            "snapshots(listing_id, rent INT, seen_at TEXT)  -- rent history\n\n"
            "RULES: SELECT only. Always filter disappeared_at IS NULL for active listings. "
            "Max 200 rows returned. If your SELECT includes an 'id' column from listings, "
            "those listings are automatically rendered as clickable cards — no need to call show_listings."
        ),
        "input_schema": {
            "type": "object",
            "required": ["sql"],
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT statement"},
            },
        },
    },
]

_23KU = [
    "千代田区","中央区","港区","新宿区","文京区","台東区","墨田区","江東区",
    "品川区","目黒区","大田区","世田谷区","渋谷区","中野区","杉並区","豊島区",
    "北区","荒川区","板橋区","練馬区","足立区","葛飾区","江戸川区",
]

def _expand_ward_alias(ward: str):
    """Return list of ward strings for known group aliases, else [ward]."""
    lower = ward.strip().lower().replace("-", "").replace("ー", "").replace("　", "")
    if lower in ("23区", "23ku", "23wards", "23区内", "東京23区", "特別区", "都内23区"):
        return _23KU
    return [ward]

def _chat_search(params, con):
    cutoff = (datetime.now() - timedelta(minutes=90)).isoformat()
    conds, args = ["disappeared_at IS NULL", "last_seen >= ?"], [cutoff]
    if params.get("name"):
        conds.append("name LIKE ?"); args.append(f"%{params['name']}%")
    # Support single ward string or wards array; expand group aliases like 23区
    raw_wards = params.get("wards") or ([params["ward"]] if params.get("ward") else [])
    expanded = []
    for w in raw_wards:
        expanded.extend(_expand_ward_alias(w))
    if expanded:
        placeholders = ",".join("?" * len(expanded))
        conds.append(f"ward IN ({placeholders})")
        args.extend(expanded)
    if params.get("layout"):
        conds.append("layout = ?"); args.append(params["layout"])
    if params.get("min_rent"):
        conds.append("rent >= ?"); args.append(params["min_rent"])
    if params.get("max_rent"):
        conds.append("rent <= ?"); args.append(params["max_rent"])
    if params.get("min_size"):
        conds.append("size_m2 >= ?"); args.append(params["min_size"])
    if params.get("max_size"):
        conds.append("size_m2 <= ?"); args.append(params["max_size"])
    if params.get("source"):
        conds.append("source = ?"); args.append(params["source"])
    if params.get("max_walk"):
        conds.append("walk_min IS NOT NULL AND walk_min <= ?"); args.append(params["max_walk"])
    if params.get("has_floor_plan"):
        conds.append("id IN (SELECT DISTINCT listing_id FROM listing_images WHERE image_type='floor_plan')")
    if params.get("has_images"):
        conds.append("id IN (SELECT DISTINCT listing_id FROM listing_images WHERE local_path IS NOT NULL)")
    if params.get("min_living_area_m2"):
        conds.append(
            "CAST(json_extract(floor_plan_data, '$.living_area_m2') AS REAL) >= ?"
        )
        args.append(float(params["min_living_area_m2"]))
    limit = min(int(params.get("limit", 10)), 200)
    sort_map = {
        "rent_asc":         "rent ASC",
        "rent_desc":        "rent DESC",
        "living_area_desc": "CAST(json_extract(floor_plan_data,'$.living_area_m2') AS REAL) DESC NULLS LAST",
        "size_desc":        "size_m2 DESC",
    }
    order = sort_map.get(params.get("sort_by", "rent_asc"), "rent ASC")
    rows = con.execute(
        f"SELECT id,name,ward,rent,layout,size_m2,url,source,nearest_station,walk_min,thumbnail_url "
        f"FROM listings WHERE {' AND '.join(conds)} ORDER BY {order} LIMIT ?",
        args + [limit]
    ).fetchall()
    return {"count": len(rows), "listings": [dict(r) for r in rows]}

def _chat_stats(con):
    total = con.execute("SELECT COUNT(*) FROM listings WHERE disappeared_at IS NULL").fetchone()[0]
    by_src = {r["source"]: r["n"] for r in con.execute(
        "SELECT source, COUNT(*) n FROM listings WHERE disappeared_at IS NULL GROUP BY source"
    ).fetchall()}
    rent = con.execute(
        "SELECT MIN(rent) mn, MAX(rent) mx, CAST(AVG(rent) AS INTEGER) avg "
        "FROM listings WHERE disappeared_at IS NULL"
    ).fetchone()
    wards = con.execute(
        "SELECT ward, COUNT(*) n FROM listings WHERE disappeared_at IS NULL "
        "GROUP BY ward ORDER BY n DESC LIMIT 10"
    ).fetchall()
    layouts = con.execute(
        "SELECT layout, COUNT(*) n FROM listings WHERE disappeared_at IS NULL "
        "AND layout IS NOT NULL GROUP BY layout ORDER BY n DESC"
    ).fetchall()
    return {
        "total": total, "by_source": dict(by_src),
        "rent": dict(rent), "top_wards": [dict(r) for r in wards],
        "layouts": [dict(r) for r in layouts],
    }

def _chat_room_stats(con):
    """Return percentile distribution of living area and common room metrics."""
    import json as _json
    rows = con.execute(
        "SELECT floor_plan_data FROM listings WHERE floor_plan_data IS NOT NULL AND disappeared_at IS NULL"
    ).fetchall()
    living, toilets, bathrooms, kitchens_open = [], [], [], []
    for r in rows:
        try:
            d = _json.loads(r["floor_plan_data"])
            if d.get("living_area_m2") is not None:
                living.append(float(d["living_area_m2"]))
            if d.get("toilet_count") is not None:
                toilets.append(int(d["toilet_count"]))
            if d.get("bathroom_count") is not None:
                bathrooms.append(int(d["bathroom_count"]))
            if d.get("kitchen_open") is not None:
                kitchens_open.append(bool(d["kitchen_open"]))
        except Exception:
            pass
    if not living:
        total_analyzed = len(rows)
        total_fp = con.execute(
            "SELECT COUNT(DISTINCT listing_id) FROM listing_images WHERE image_type='floor_plan'"
        ).fetchone()[0]
        return {
            "message": "No room analysis data yet. Run analyze_floor_plans.py to populate.",
            "floor_plans_in_db": total_fp,
            "analyzed": total_analyzed,
        }
    living.sort()
    n = len(living)
    def pct(arr, p):
        idx = min(int(len(arr) * p / 100), len(arr) - 1)
        return round(arr[idx], 1)
    return {
        "analyzed_count": n,
        "living_area_m2": {
            "min":    round(min(living), 1),
            "p25":    pct(living, 25),
            "median": pct(living, 50),
            "p75":    pct(living, 75),
            "p90":    pct(living, 90),
            "max":    round(max(living), 1),
            "note": "Use p75 as threshold for 'large', p90 for 'very large/huge' living rooms",
        },
        "toilet_count_distribution": {str(k): toilets.count(k) for k in sorted(set(toilets))},
        "bathroom_count_distribution": {str(k): bathrooms.count(k) for k in sorted(set(bathrooms))},
        "open_kitchen_pct": round(100 * sum(kitchens_open) / len(kitchens_open)) if kitchens_open else None,
    }


def _chat_show_images(params, con):
    lid = params.get("listing_id", "")
    filter_type = params.get("image_type", "")
    if filter_type:
        rows = con.execute(
            "SELECT id, url, image_type, local_path FROM listing_images "
            "WHERE listing_id=? AND image_type=? ORDER BY id",
            (lid, filter_type),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT id, url, image_type, local_path FROM listing_images WHERE listing_id=? "
            "ORDER BY CASE image_type WHEN 'floor_plan' THEN 0 WHEN 'exterior' THEN 1 ELSE 2 END, id",
            (lid,),
        ).fetchall()
    if not rows:
        return {"error": f"no {''+filter_type+' ' if filter_type else ''}images found for this listing"}
    images = []
    for r in rows:
        serve_url = f"/api/images/{r['id']}" if r["local_path"] else r["url"]
        images.append({"url": serve_url, "type": r["image_type"]})
    _row = con.execute("SELECT name FROM listings WHERE id=?", (lid,)).fetchone()
    name = _row["name"] if _row else ""
    return {
        "count": len(images),
        "listing_name": name,
        "images": images,
        "rendered": True,
        "instruction": "Images are now displayed in the chat UI. Do NOT describe, list, or narrate them in your text response. Say only the listing name if needed, then stop.",
    }


def _chat_batch_commute(params, con):
    destination = params.get("destination", "")
    max_minutes = int(params.get("max_minutes", 60))
    if not destination:
        return {"error": "destination required"}
    if not GOOGLE_MAPS_SERVER_KEY:
        return {"error": "Google Maps API key not configured"}

    conds, args = ["disappeared_at IS NULL", "lat IS NOT NULL", "lng IS NOT NULL"], []
    if params.get("source"):
        conds.append("source = ?"); args.append(params["source"])
    listing_ids = params.get("listing_ids") or []
    if listing_ids:
        placeholders = ",".join("?" * len(listing_ids))
        conds.append(f"id IN ({placeholders})")
        args.extend(listing_ids)
    rows = con.execute(
        f"SELECT id, name, ward, rent, layout, source, lat, lng FROM listings WHERE {' AND '.join(conds)}",
        args,
    ).fetchall()
    if not rows:
        return {"error": "No geocoded listings found"}

    # Use next weekday 9 AM for representative rush-hour transit
    now = datetime.now()
    dep = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if dep <= now:
        dep += timedelta(days=1)
    while dep.weekday() >= 5:
        dep += timedelta(days=1)
    dep_ts = int(dep.timestamp())

    matching = []
    BATCH = 25
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        origins = "|".join(f"{r['lat']},{r['lng']}" for r in batch)
        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={"origins": origins, "destinations": destination,
                        "mode": "transit", "departure_time": dep_ts,
                        "language": "ja", "region": "JP",
                        "key": GOOGLE_MAPS_SERVER_KEY},
                timeout=30,
            ).json()
        except Exception as e:
            logging.warning(f"Distance Matrix batch {i} failed: {e}")
            continue
        if resp.get("status") != "OK":
            logging.warning(f"Distance Matrix status: {resp.get('status')}")
            continue
        for j, row_data in enumerate(resp.get("rows", [])):
            el = (row_data.get("elements") or [{}])[0]
            if el.get("status") == "OK":
                minutes = round(el["duration"]["value"] / 60)
                if minutes <= max_minutes:
                    r = batch[j]
                    matching.append({
                        "id": r["id"], "name": r["name"], "ward": r["ward"],
                        "rent": r["rent"], "layout": r["layout"], "source": r["source"],
                        "commute_minutes": minutes,
                    })

    matching.sort(key=lambda x: x["commute_minutes"])
    return {
        "count": len(matching),
        "checked": len(rows),
        "destination": destination,
        "max_minutes": max_minutes,
        "listings": matching,
    }


def _chat_run_sql(params, con):
    import re as _re
    sql = (params.get("sql") or "").strip().rstrip(";")
    if not sql:
        return {"error": "no sql provided"}
    first_word = _re.split(r'\s+', sql, maxsplit=1)[0].upper()
    if first_word != "SELECT":
        return {"error": "only SELECT statements are allowed"}
    blocked = ("users", "user_settings", "user_notifications", "chat_messages", "chat_sessions")
    lower = sql.lower()
    for t in blocked:
        if _re.search(rf'\b{t}\b', lower):
            return {"error": f"access to table '{t}' is not allowed"}
    if "limit" not in lower:
        sql = f"{sql} LIMIT 200"
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(200)
        return {"columns": cols, "rows": [list(r) for r in rows], "count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


def _chat_market_trends(params, con):
    conds, args = ["disappeared_at IS NOT NULL", "first_seen IS NOT NULL"], []
    if params.get("ward"):
        conds.append("ward LIKE ?"); args.append(f"%{params['ward']}%")
    if params.get("source"):
        conds.append("source = ?"); args.append(params["source"])
    where = " AND ".join(conds)
    by_source = con.execute(
        f"""SELECT source, COUNT(*) n,
            ROUND(AVG(julianday(disappeared_at)-julianday(first_seen)),1) avg_days,
            ROUND(MIN(julianday(disappeared_at)-julianday(first_seen)),1) min_days,
            ROUND(MAX(julianday(disappeared_at)-julianday(first_seen)),1) max_days
           FROM listings WHERE {where} GROUP BY source ORDER BY source""",
        args,
    ).fetchall()
    by_ward = con.execute(
        f"""SELECT ward, source, COUNT(*) n,
            ROUND(AVG(julianday(disappeared_at)-julianday(first_seen)),1) avg_days
           FROM listings WHERE {where}
           GROUP BY ward, source ORDER BY n DESC LIMIT 20""",
        args,
    ).fetchall()
    return {
        "total_historical": sum(r["n"] for r in by_source),
        "by_source": [dict(r) for r in by_source],
        "by_ward":   [dict(r) for r in by_ward],
    }


def _chat_details(params, con):
    row = con.execute(
        "SELECT * FROM listings WHERE id=? AND disappeared_at IS NULL",
        (params.get("listing_id",""),)
    ).fetchone()
    if not row:
        return {"error": "listing not found or no longer available"}
    facs = con.execute(
        "SELECT type,name,distance_m FROM listing_facilities WHERE listing_id=? ORDER BY distance_m LIMIT 12",
        (params["listing_id"],)
    ).fetchall()
    # Include any OCR text extracted from floor plan images
    ocr_rows = con.execute(
        "SELECT ocr_text FROM listing_images WHERE listing_id=? AND image_type='floor_plan' AND ocr_text IS NOT NULL",
        (params["listing_id"],)
    ).fetchall()
    ocr_texts = [r["ocr_text"] for r in ocr_rows if r["ocr_text"]]
    result = {**dict(row), "facilities": [dict(f) for f in facs]}
    if ocr_texts:
        result["floor_plan_ocr"] = "\n---\n".join(ocr_texts)
    if row["floor_plan_data"]:
        try:
            import json as _json
            result["floor_plan_rooms"] = _json.loads(row["floor_plan_data"])
        except Exception:
            pass
    return result

import re as _re
_PHOTO_RE = _re.compile(
    r'(show|see|view|display|open|get|fetch|load|give|表示|見せ|見たい|みたい|写真|画像|間取り|フロアプラン|floor\s*plan|photo|image|picture|gallery)',
    _re.IGNORECASE,
)

def _is_photo_request(history):
    """Return (True, image_type_filter) if the last user message is a photo/floor-plan request."""
    last = next((m for m in reversed(history) if m["role"] == "user"), None)
    if not last:
        return False, None
    txt = last["content"] if isinstance(last["content"], str) else ""
    if not _PHOTO_RE.search(txt):
        return False, None
    lower = txt.lower()
    if any(w in lower for w in ("floor plan", "間取り", "フロアプラン", "floor_plan")):
        return True, "floor_plan"
    if any(w in lower for w in ("exterior", "外観", "外装")):
        return True, "exterior"
    if any(w in lower for w in ("interior", "室内", "内装")):
        return True, "interior"
    return True, None

def _call_claude(history, con, open_listing=None, user_id=None, saved_listings=None):
    if not _anthropic:
        return {"text": "anthropic パッケージが未インストールです: pip install anthropic", "listing_ids": [], "actions": []}
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"text": "ANTHROPIC_API_KEY が .env に設定されていません。", "listing_ids": [], "actions": []}

    # Fast path: if user is asking for photos and a listing is open, skip the LLM
    is_photo, photo_type = _is_photo_request(history)
    if is_photo and open_listing:
        params = {"listing_id": open_listing["id"]}
        if photo_type:
            params["image_type"] = photo_type
        result = _chat_show_images(params, con)
        if "images" in result:
            return {
                "text": "",
                "listing_ids": [],
                "actions": [{"type": "show_images", "listing_name": result["listing_name"], "images": result["images"]}],
            }

    client = _anthropic.Anthropic(api_key=api_key)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    all_ids = []
    all_actions = []

    if open_listing:
        open_ctx = (
            f"Currently open property card: {open_listing['name']} "
            f"({open_listing['ward']}, {open_listing['layout']}, "
            f"¥{open_listing['rent']:,}/月, {open_listing['size_m2']}m²) — listing_id: {open_listing['id']}\n"
        )
    else:
        open_ctx = ""

    if saved_listings:
        lines = [f"User's saved selections ({len(saved_listings)} listings):"]
        for s in saved_listings:
            lines.append(
                f"  - {s['name']} ({s['ward']}, {s['layout']}, ¥{s['rent']:,}) "
                f"id={s['id']} photos={s['photo_count']}"
            )
        open_ctx += "\n".join(lines) + "\n"

    for _ in range(10):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=CHAT_SYSTEM.format(date=datetime.now().strftime("%Y-%m-%d"), open_listing_context=open_ctx),
            tools=_CHAT_TOOLS,
            messages=messages,
        )
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            # Images are self-displaying — suppress AI text narration when photos were shown
            if any(a.get("type") == "show_images" for a in all_actions):
                text = ""
            return {"text": text, "listing_ids": all_ids, "actions": all_actions}

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "search_listings":
                result = _chat_search(block.input, con)
                all_ids.extend(r["id"] for r in result.get("listings", []))
            elif block.name == "get_statistics":
                result = _chat_stats(con)
            elif block.name == "get_room_stats":
                result = _chat_room_stats(con)
            elif block.name == "show_listing_images":
                result = _chat_show_images(block.input, con)
                if "images" in result:
                    all_actions.append({
                        "type": "show_images",
                        "listing_name": result.get("listing_name", ""),
                        "images": result["images"],
                    })
            elif block.name == "batch_commute_filter":
                result = _chat_batch_commute(block.input, con)
                all_ids.extend(r["id"] for r in result.get("listings", []))
            elif block.name == "save_listings":
                ids = block.input.get("listing_ids", [])
                if user_id and ids:
                    now_iso = datetime.now().isoformat()
                    for lid in ids:
                        con.execute(
                            "INSERT OR IGNORE INTO user_saved_listings (user_id, listing_id) VALUES (?,?)",
                            (user_id, lid),
                        )
                    con.commit()
                    all_actions.append({"type": "save_listings", "listing_ids": ids})
                    all_ids.extend(ids)
                    result = {"saved": len(ids)}
                else:
                    result = {"error": "not logged in" if not user_id else "no listing_ids provided"}
            elif block.name == "remove_listings":
                ids = block.input.get("listing_ids", [])
                if user_id and ids:
                    for lid in ids:
                        con.execute(
                            "DELETE FROM user_saved_listings WHERE user_id=? AND listing_id=?",
                            (user_id, lid),
                        )
                    con.commit()
                    all_actions.append({"type": "remove_listings", "listing_ids": ids})
                    result = {"removed": len(ids)}
                else:
                    result = {"error": "not logged in" if not user_id else "no listing_ids provided"}
            elif block.name == "run_sql":
                result = _chat_run_sql(block.input, con)
                if "columns" in result:
                    cols = result["columns"]
                    id_col = next((c for c in cols if c in ("id", "listing_id")), None)
                    if id_col:
                        id_idx = cols.index(id_col)
                        rendered = [r[id_idx] for r in result["rows"] if r[id_idx]]
                        all_ids.extend(rendered)
                        # Don't echo rows back — just tell the model how many cards rendered
                        other_cols = [c for c in cols if c not in ("id", "listing_id")]
                        result = {"rendered_cards": len(rendered), "columns": other_cols,
                                  "sample": [[r[i] for i, c in enumerate(cols) if c not in ("id","listing_id")]
                                             for r in result["rows"][:3]]}
            elif block.name == "get_market_trends":
                result = _chat_market_trends(block.input, con)
            elif block.name == "show_listings":
                ids = block.input.get("listing_ids", [])
                placeholders = ",".join("?" * len(ids)) if ids else "NULL"
                rows = con.execute(
                    f"SELECT id FROM listings WHERE id IN ({placeholders})", ids
                ).fetchall() if ids else []
                valid = [r["id"] for r in rows]
                all_ids.extend(valid)
                result = {"rendered": len(valid)}
            elif block.name == "get_listing_details":
                result = _chat_details(block.input, con)
            elif block.name == "get_commute_time":
                inp  = block.input
                lid  = inp.get("listing_id", "")
                dest = inp.get("destination", "")
                mode = inp.get("mode", "transit")
                row  = con.execute(
                    "SELECT lat, lng, name, nearest_station FROM listings WHERE id=?", (lid,)
                ).fetchone()
                if not row or not row["lat"]:
                    result = {"error": "listing not geocoded"}
                elif mode != "transit":
                    # Driving / walking: resolve server-side
                    try:
                        gm = requests.get(
                            "https://maps.googleapis.com/maps/api/directions/json",
                            params={"origin": f"{row['lat']},{row['lng']}", "destination": dest,
                                    "mode": mode, "language": "ja", "region": "JP",
                                    "key": GOOGLE_MAPS_SERVER_KEY},
                            timeout=10,
                        ).json()
                        if gm.get("status") == "OK":
                            leg = gm["routes"][0]["legs"][0]
                            result = {"duration": leg["duration"]["text"],
                                      "duration_minutes": round(leg["duration"]["value"] / 60),
                                      "distance": leg["distance"]["text"], "mode_used": mode}
                            all_actions.append({
                                "type":        "show_driving_route",
                                "lat":         row["lat"], "lng": row["lng"],
                                "destination": dest,
                                "mode":        mode.upper(),
                            })
                        else:
                            result = {"error": f"No route ({gm.get('status')})"}
                    except Exception as e:
                        result = {"error": str(e)}
                else:
                    # Transit: Japan transit data unavailable server-side — delegate to client Maps JS API
                    all_actions.append({
                        "type":         "get_commute_client",
                        "lat":          row["lat"],
                        "lng":          row["lng"],
                        "destination":  dest,
                        "listing_name": row["name"],
                    })
                    result = {"status": "calculating", "message": "Transit route is being fetched by the map — result will appear in chat momentarily."}
            elif block.name == "center_map":
                inp = block.input
                lat, lng = inp.get("lat"), inp.get("lng")
                if not lat and inp.get("listing_id"):
                    row = con.execute(
                        "SELECT lat, lng FROM listings WHERE id=?", (inp["listing_id"],)
                    ).fetchone()
                    if row:
                        lat, lng = row["lat"], row["lng"]
                if lat and lng:
                    all_actions.append({
                        "type": "center_map", "lat": lat, "lng": lng,
                        "zoom": int(inp.get("zoom") or 15),
                    })
                    result = {"ok": True, "lat": lat, "lng": lng}
                else:
                    result = {"error": "coordinates not found"}
            elif block.name == "select_listing":
                lid = block.input.get("listing_id", "")
                all_actions.append({"type": "select_listing", "listing_id": lid})
                result = {"ok": True}
            elif block.name == "pick_listing":
                all_actions.append({"type": "await_card_pick", "pending_message": block.input.get("pending_message", "")})
                result = {"ok": True}
            elif block.name == "set_map_filter":
                inp = block.input
                action = {"type": "set_map_filter"}
                for key in ("min_size", "max_size", "min_rent", "max_rent", "max_walk", "source"):
                    if key in inp:
                        action[key] = inp[key]
                all_actions.append(action)
                result = {"ok": True, "filter": action}
            elif block.name == "find_nearby_place":
                inp        = block.input
                lid        = inp.get("listing_id", "")
                place_type = inp.get("place_type", "")
                keyword    = inp.get("keyword", "")
                radius     = min(int(inp.get("radius_m", 3000)), 10000)
                row = con.execute(
                    "SELECT lat, lng FROM listings WHERE id=?", (lid,)
                ).fetchone()
                if not row or not row["lat"]:
                    result = {"error": "listing not geocoded"}
                else:
                    # Check OSM-cached facility data first (fast, free, consistent with UI)
                    cached = con.execute(
                        "SELECT name, distance_m FROM listing_facilities "
                        "WHERE listing_id=? AND type=? AND distance_m<=? "
                        "ORDER BY distance_m",
                        (lid, place_type, radius),
                    ).fetchall()
                    if cached:
                        result = {"places": [{"name": r["name"], "distance_m": r["distance_m"]} for r in cached],
                                  "source": "cached"}
                    elif not GOOGLE_MAPS_SERVER_KEY:
                        result = {"message": f"No {place_type} found in cached data within {radius}m"}
                    else:
                        try:
                            params = {"location": f"{row['lat']},{row['lng']}",
                                      "radius": radius, "type": place_type,
                                      "language": "ja", "key": GOOGLE_MAPS_SERVER_KEY}
                            if keyword:
                                params["keyword"] = keyword
                            r = requests.get(
                                "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                                params=params,
                                timeout=10,
                            ).json()
                            if r.get("status") in ("OK", "ZERO_RESULTS"):
                                def _haversine(lat1, lng1, lat2, lng2):
                                    dlat = (lat2 - lat1) * math.pi / 180
                                    dlng = (lng2 - lng1) * math.pi / 180
                                    a = (math.sin(dlat/2)**2
                                         + math.cos(lat1*math.pi/180) * math.cos(lat2*math.pi/180)
                                         * math.sin(dlng/2)**2)
                                    return round(6371000 * 2 * math.asin(math.sqrt(a)))
                                places = []
                                for p in (r.get("results") or [])[:10]:
                                    loc = p["geometry"]["location"]
                                    places.append({
                                        "name":       p["name"],
                                        "address":    p.get("vicinity", ""),
                                        "lat":        loc["lat"], "lng": loc["lng"],
                                        "distance_m": _haversine(row["lat"], row["lng"],
                                                                 loc["lat"], loc["lng"]),
                                    })
                                places.sort(key=lambda x: x["distance_m"])
                                result = {"places": places, "source": "google"} if places else {
                                    "message": f"No {place_type} found within {radius}m"}
                            else:
                                result = {"error": f"Places API: {r.get('status')}"}
                        except Exception as e:
                            result = {"error": str(e)}
            else:
                result = {"error": "unknown tool"}
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": tool_results})

    return {"text": "応答の生成に失敗しました。もう一度お試しください。", "listing_ids": [], "actions": all_actions}


@app.route("/api/chat", methods=["POST"])
@jwt_required(optional=True)
def chat_send():
    body       = request.get_json(silent=True) or {}
    message    = (body.get("message") or "").strip()
    session_id = body.get("session_id")
    if not message:
        return jsonify({"error": "message required"}), 400

    user_id = get_jwt_identity()
    con = db()

    if not session_id:
        con.execute("INSERT INTO chat_sessions (user_id) VALUES (?)", (user_id,))
        con.commit()
        session_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]

    rows = con.execute(
        "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY created_at DESC LIMIT 15",
        (session_id,)
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    history.append({"role": "user", "content": message})

    con.execute(
        "INSERT INTO chat_messages (session_id, role, content) VALUES (?,?,?)",
        (session_id, "user", message)
    )
    con.commit()

    open_listing_id = body.get("open_listing_id")
    open_listing = None
    if open_listing_id:
        row = con.execute(
            "SELECT id, name, ward, layout, rent, size_m2 FROM listings WHERE id=?",
            (open_listing_id,)
        ).fetchone()
        if row:
            open_listing = dict(row)

    saved_rows = con.execute(
        """SELECT l.id, l.name, l.ward, l.rent, l.layout,
                  COUNT(li.id) as photo_count
           FROM user_saved_listings s
           JOIN listings l ON l.id = s.listing_id
           LEFT JOIN listing_images li ON li.listing_id = l.id AND li.local_path IS NOT NULL
           WHERE s.user_id=?
           GROUP BY l.id ORDER BY s.saved_at DESC""",
        (user_id,)
    ).fetchall() if user_id else []
    saved_listings_ctx = [dict(r) for r in saved_rows]

    result = _call_claude(history, con, open_listing=open_listing, user_id=user_id, saved_listings=saved_listings_ctx)

    con.execute(
        "INSERT INTO chat_messages (session_id, role, content, listing_ids) VALUES (?,?,?,?)",
        (session_id, "assistant", result["text"], json.dumps(result["listing_ids"]))
    )
    con.execute(
        """DELETE FROM chat_messages WHERE session_id=? AND id NOT IN (
               SELECT id FROM chat_messages WHERE session_id=? ORDER BY created_at DESC LIMIT 100
           )""",
        (session_id, session_id)
    )
    con.commit()
    con.close()

    return jsonify({"session_id": session_id, "response": result["text"],
                    "listing_ids": result["listing_ids"],
                    "actions": result.get("actions", [])})


@app.route("/api/chat/history")
@jwt_required(optional=True)
def chat_history():
    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"messages": []})
    con = db()
    rows = con.execute(
        "SELECT role, content, listing_ids, created_at FROM chat_messages "
        "WHERE session_id=? ORDER BY created_at",
        (session_id,)
    ).fetchall()
    con.close()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.route("/api/chat/sessions")
@jwt_required(optional=True)
def chat_sessions_list():
    user_id = get_jwt_identity()
    if not user_id:
        return jsonify({"sessions": []})
    con = db()
    rows = con.execute(
        "SELECT id, title, created_at FROM chat_sessions "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (user_id,)
    ).fetchall()
    con.close()
    return jsonify({"sessions": [dict(r) for r in rows]})


@app.route("/api/search/status")
def search_status():
    con = db()
    total   = con.execute("SELECT COUNT(*) FROM listings WHERE disappeared_at IS NULL").fetchone()[0]
    indexed = con.execute("SELECT COUNT(*) FROM listing_embeddings").fetchone()[0]
    con.close()
    return jsonify({"total_listings": total, "indexed": indexed})


@app.route("/api/search/text")
def search_text():
    q     = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 30)), 50)
    if not q:
        return jsonify({"results": []})
    terms = q.split()
    conditions, params = [], []
    for term in terms:
        like = f"%{term}%"
        conditions.append("(name LIKE ? OR ward LIKE ? OR address LIKE ? OR layout LIKE ?)")
        params.extend([like, like, like, like])
    where = " AND ".join(conditions)
    con = db()
    rows = con.execute(
        f"SELECT * FROM listings WHERE disappeared_at IS NULL AND {where} "
        f"ORDER BY last_seen DESC LIMIT ?",
        params + [limit]
    ).fetchall()
    results = [dict(r) for r in rows]
    con.close()
    return jsonify({"results": results})


@app.route("/api/search/visual", methods=["POST"])
def search_visual():
    """
    Vector similarity search over CLIP-embedded listings.

    Body JSON:
        { "vec": [f32, f32, ...],   // 512-dim CLIP vector from embedder.py
          "limit": 20 }             // optional, max 50

    The Mac-side embedder.py generates the query vector; this endpoint
    just does cosine similarity over stored embeddings (no ML on server).
    """
    import struct
    try:
        import numpy as np
    except ImportError:
        return jsonify({"error": "numpy not installed on server"}), 501

    body  = request.get_json(silent=True) or {}
    vec   = body.get("vec")
    limit = min(int(body.get("limit", 20)), 50)

    if not vec or len(vec) != 512:
        return jsonify({"error": "vec must be a 512-element float array"}), 400

    q_vec = np.array(vec, dtype=np.float32)
    q_vec /= (np.linalg.norm(q_vec) + 1e-9)

    con = db()
    emb_rows = con.execute(
        "SELECT listing_id, embedding FROM listing_embeddings"
    ).fetchall()

    if not emb_rows:
        con.close()
        return jsonify({"results": [], "total_indexed": 0})

    ids   = [r["listing_id"] for r in emb_rows]
    vecs  = np.stack([
        np.frombuffer(bytes(r["embedding"]), dtype=np.float32) for r in emb_rows
    ])
    scores = (vecs @ q_vec).tolist()

    ranked = sorted(zip(ids, scores), key=lambda x: -x[1])[:limit]

    results = []
    for lid, score in ranked:
        row = con.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        if row:
            results.append({"score": round(score, 4), **dict(row)})

    con.close()
    return jsonify({"results": results, "total_indexed": len(emb_rows)})


@app.route("/")
def serve_desktop():
    import os
    return send_file(os.path.join(os.path.dirname(__file__), "dashboard.html"))

@app.route("/m")
def serve_mobile():
    import os
    return send_file(os.path.join(os.path.dirname(__file__), "dashboard-mobile.html"))


if __name__ == "__main__":
    con = db()  # init tables + JWT secret on startup
    # Kick worker if geocoded listings exist with no facilities (e.g. after migration wipe)
    needs = con.execute(
        "SELECT COUNT(*) FROM listings WHERE lat IS NOT NULL "
        "AND id NOT IN (SELECT DISTINCT listing_id FROM listing_facilities)"
    ).fetchone()[0]
    con.close()
    if needs:
        logging.info(f"Startup: {needs} listings need facility fetch, starting worker")
        _ensure_geocoding()
    app.run(host="0.0.0.0", port=5050, debug=False)
