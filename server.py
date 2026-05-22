"""
Config + read API for the dashboard. Run alongside scraper4.py.
"""

from flask import Flask, jsonify, request, redirect
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity
)
from werkzeug.security import generate_password_hash, check_password_hash
import json, logging, math, os, secrets, sqlite3, time, threading, requests
from datetime import datetime, timedelta
from urllib.parse import urlencode
from dotenv import load_dotenv

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
    ]:
        if col not in cols:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {defn}")

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

    # Migrate: add map_layers column if it doesn't exist yet
    try:
        con.execute("ALTER TABLE user_settings ADD COLUMN map_layers TEXT DEFAULT '{}'")
        con.commit()
    except Exception:
        pass  # column already exists

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
    ("railway", "subway_entrance", "station"),
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

    con = db()
    try:
        con.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (?,?,?)",
            (email, generate_password_hash(password), display_name or email.split("@")[0])
        )
        con.commit()
        user = con.execute("SELECT id, email, display_name FROM users WHERE email=?", (email,)).fetchone()
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

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "メールアドレスまたはパスワードが間違っています"}), 401

    token = create_access_token(identity=str(user["id"]))
    return jsonify({"token": token, "email": user["email"], "display_name": user["display_name"]})


@app.route("/api/auth/me")
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    con     = db()
    user    = con.execute(
        "SELECT id, email, display_name, created_at FROM users WHERE id=?", (user_id,)
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


# ── Listing facilities ────────────────────────────────────────────────────────

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
        "       disappeared_at, walk_min, walk_m, nearest_station "
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


if __name__ == "__main__":
    db()  # init tables + JWT secret on startup
    app.run(host="0.0.0.0", port=5050, debug=False)
