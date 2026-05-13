"""
Config + read API for the dashboard. Run alongside scraper3.py.

  pip install flask flask-cors requests
  python server.py
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import json, math, os, sqlite3, time, threading, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

CONFIG_FILE = "config.json"
LOG_FILE    = "jkk_monitor.log"
DB_FILE     = "jkk_monitor.db"

app = Flask(__name__)
CORS(app)

TOKYO_WARDS = [
    "千代田区","中央区","港区","新宿区","文京区","台東区","墨田区","江東区",
    "品川区","目黒区","大田区","世田谷区","渋谷区","中野区","杉並区","豊島区",
    "北区","荒川区","板橋区","練馬区","足立区","葛飾区","江戸川区"
]

LAYOUTS = ["1R","1K","1DK","1LDK","2K","2DK","2LDK","3K","3DK","3LDK","4LDK以上"]

# Approximate ward/city centroids — used as a fallback when GSI can't
# resolve a building-level address.
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
    # Kanagawa — Yokohama wards
    "横浜市鶴見区":   (35.5084,139.6761),"横浜市神奈川区":(35.4890,139.6339),
    "横浜市西区":     (35.4666,139.6218),"横浜市中区":    (35.4437,139.6427),
    "横浜市南区":     (35.4255,139.6145),"横浜市保土ケ谷区":(35.4607,139.5952),
    "横浜市磯子区":   (35.3990,139.6327),"横浜市金沢区":  (35.3534,139.6284),
    "横浜市港北区":   (35.5300,139.6305),"横浜市戸塚区":  (35.3975,139.5332),
    "横浜市港南区":   (35.3953,139.5965),"横浜市旭区":    (35.4621,139.5592),
    "横浜市緑区":     (35.5100,139.5872),"横浜市瀬谷区":  (35.4630,139.5089),
    "横浜市栄区":     (35.3679,139.5737),"横浜市青葉区":  (35.5560,139.5479),
    "横浜市都筑区":   (35.5399,139.5763),
    # Kanagawa — Kawasaki wards
    "川崎市川崎区":   (35.5308,139.6974),"川崎市幸区":    (35.5389,139.6726),
    "川崎市中原区":   (35.5731,139.6615),"川崎市高津区":  (35.6020,139.6430),
    "川崎市麻生区":   (35.6451,139.4998),"川崎市多摩区":  (35.6118,139.5505),
    "川崎市宮前区":   (35.5885,139.5742),
    # Kanagawa — Sagamihara
    "相模原市緑区":   (35.5968,139.3906),"相模原市中央区":(35.5716,139.3720),
    "相模原市南区":   (35.5298,139.3888),
}

# Geocoding runs in a background daemon thread so the listings endpoint
# never blocks waiting for GSI. The dashboard polls until pending == 0.
GEOCODE_TIMEOUT_S    = 5
GEOCODE_DELAY_S      = 0.3
GEOCODE_LOOKBACK_HOURS = 6
GSI_URL        = "https://msearch.gsi.go.jp/address-search/AddressSearch"
HEARTRAILS_URL = "https://express.heartrails.com/api/json"
HEARTRAILS_TIMEOUT_S = 5

_geo_lock = threading.Lock()
_geo_running = False


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    # Idempotent migration so the dashboard works even if the scraper
    # hasn't been started since these columns were added.
    cols = {row[1] for row in con.execute("PRAGMA table_info(listings)")}
    if "source" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN source TEXT DEFAULT 'jkk'")
    if "address" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN address TEXT")
    if "lat" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN lat REAL")
    if "lng" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN lng REAL")
    if "geocoded_at" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN geocoded_at TEXT")
    if "disappeared_at" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN disappeared_at TEXT")
    if "walk_min" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN walk_min INTEGER")
    if "walk_m" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN walk_m INTEGER")
    if "nearest_station" not in cols:
        con.execute("ALTER TABLE listings ADD COLUMN nearest_station TEXT")
    con.commit()
    return con


def _gsi_geocode(query):
    try:
        r = requests.get(
            GSI_URL,
            params={"q": query},
            timeout=GEOCODE_TIMEOUT_S,
            headers={"User-Agent": "jkktrackr/1.0 (dashboard)"},
        )
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
    # GSI sometimes returns clearly bogus coordinates outside Japan;
    # constrain to a generous Kanto bounding box.
    if not (35.0 <= lat <= 36.5 and 138.5 <= lng <= 140.5):
        return None
    return (float(lat), float(lng))


def _nearest_station(lat, lng):
    """Return (label, walk_min, dist_m) for the closest train station via HeartRails.

    label is e.g. "渋谷（東急東横線）".
    walk_min uses the standard Japanese 1 min / 80 m rule (ceil, min 1).
    Returns None on API failure.
    """
    try:
        r = requests.get(
            HEARTRAILS_URL,
            params={"method": "getStations", "x": str(lng), "y": str(lat)},
            timeout=HEARTRAILS_TIMEOUT_S,
            headers={"User-Agent": "jkktrackr/1.0 (dashboard)"},
        )
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
    """Return the prefecture string to prepend to geocode queries."""
    if any(ward.startswith(p) for p in _KANAGAWA_PREFIXES):
        return "神奈川県"
    return "東京都"

def _build_geocode_query(row):
    name = (row["name"] or "").strip()
    address = (row["address"] or "").strip()
    ward = (row["ward"] or "").strip()
    pref = _prefecture(ward)
    # UR listings carry a real street address in `address`. JKK only has
    # the ward, so we lean on the building name plus prefecture for those.
    if address and address != ward:
        return f"{pref}{address}"
    if name:
        return f"{pref}{ward} {name}"
    return f"{pref}{ward}"


def _geocode_worker():
    """Drain geocode-pending rows, then station-lookup-pending rows."""
    global _geo_running
    try:
        while True:
            con = sqlite3.connect(DB_FILE)
            con.row_factory = sqlite3.Row
            cutoff = (datetime.now() - timedelta(hours=GEOCODE_LOOKBACK_HOURS)).isoformat()

            # ── Pass 1: geocode a row that has no coordinates yet ──────────
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
                    station = _nearest_station(coords[0], coords[1])
                    label    = station[0] if station else None
                    walk_min = station[1] if station else None
                    walk_m   = station[2] if station else None
                    con.execute(
                        "UPDATE listings "
                        "   SET lat=?,lng=?,geocoded_at=?,nearest_station=?,walk_min=?,walk_m=?"
                        " WHERE id=?",
                        (coords[0], coords[1], now, label, walk_min, walk_m, row["id"]),
                    )
                else:
                    con.execute(
                        "UPDATE listings SET geocoded_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                con.commit()
                con.close()
                time.sleep(GEOCODE_DELAY_S)
                continue

            # ── Pass 2: nearest-station lookup for already-geocoded rows ───
            row = con.execute(
                "SELECT id, lat, lng FROM listings "
                " WHERE last_seen >= ? AND lat IS NOT NULL AND (nearest_station IS NULL OR nearest_station = '') "
                " LIMIT 1",
                (cutoff,),
            ).fetchone()
            if row:
                station = _nearest_station(row["lat"], row["lng"])
                if station:
                    con.execute(
                        "UPDATE listings SET nearest_station=?, walk_min=?, walk_m=? WHERE id=?",
                        (station[0], station[1], station[2], row["id"]),
                    )
                else:
                    # Mark attempted so we don't retry endlessly
                    con.execute(
                        "UPDATE listings SET nearest_station='' WHERE id=?", (row["id"],)
                    )
                con.commit()
                con.close()
                time.sleep(GEOCODE_DELAY_S)
                continue

            con.close()
            return
    except Exception:
        # Daemon thread — swallow errors so a single bad row doesn't
        # poison the worker; next /api/listings call will restart it.
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


@app.route("/api/config", methods=["GET"])
def get_config():
    cfg = load_config()
    cfg.pop("slack_webhook", None)  # never expose secret over API
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json()
    data.pop("slack_webhook", None)  # ignore any webhook sent from browser
    save_config(data)
    return jsonify({"status": "ok"})


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


@app.route("/api/listings")
def get_listings():
    """Return listings still considered available.

    Query params:
      max_age_min — listings whose last_seen is within this many minutes
                    of now are returned (default 90).
    """
    if not os.path.exists(DB_FILE):
        return jsonify({"listings": [], "geocoded_now": 0})

    max_age_min = request.args.get("max_age_min", default=90, type=int)
    cutoff = (datetime.now() - timedelta(minutes=max_age_min)).isoformat()

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
    """Return recently disappeared listings with their market duration."""
    if not os.path.exists(DB_FILE):
        return jsonify({"listings": []})
    limit = request.args.get("limit", default=30, type=int)
    con = db()
    cur = con.execute(
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


if __name__ == "__main__":
    app.run(port=5050, debug=False)
