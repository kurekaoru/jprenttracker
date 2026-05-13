"""
JKK Tokyo Housing Monitor

Flow:
  Splash → popup (condition search form) → check all wards → submit → results

The condition search page (akiyaJyoukenStartInit) has a single form with
checkboxes for all wards, layouts, rent range etc. We tick "select all" and
submit once to get all listings — no need to iterate areas separately.

Setup:
    pip install playwright requests beautifulsoup4 flask flask-cors
    playwright install chromium
    export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
    python scraper.py
"""

import json, time, os, re, hashlib, requests, sqlite3
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from dotenv import load_dotenv

load_dotenv()

from scraper_ur import fetch_ur_listings

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("jkk_monitor.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL  = os.environ.get("SLACK_WEBHOOK_URL", "")
CONFIG_FILE        = "config.json"
SEEN_FILE          = "seen_listings.json"
POLL_INTERVAL      = 1800

JKK_ENTRY = "https://jhomes.to-kousya.or.jp/search/jkknet/service/akiyaJyoukenStartInit"
JKK_BASE  = "https://jhomes.to-kousya.or.jp"
JKK_TOP   = "https://www.to-kousya.or.jp/chintai/index.html"

# Ward checkbox values from the form
ALL_KU_VALUES = [
    "21","11","09","07","01","05","18","22","13","12","16","03",
    "19","17","04","06","14","10","23","08","15","02","20"
]
ALL_SI_VALUES = [
    "37","51","49","31","63","43","56-64","45","32","57","65","62",
    "55","40","54","52","36","34","35","44","38","50","48","33","66",
    "41","46-47","42","39","53"
]

# Layout checkbox values: 1=1R~1LDK, 2=2K~2LDK, 3=3K~3LDK, 4=4K+
ALL_LAYOUT_VALUES = ["1","2","3","4"]

DB_FILE = "jkk_monitor.db"

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def listing_id(listing):
    src = listing.get("source", "jkk")
    key = f"{src}-{listing.get('name','')}-{listing.get('address','')}-{listing.get('layout','')}-{listing.get('rent','')}"
    return hashlib.md5(key.encode()).hexdigest()

def init_db():
    con = sqlite3.connect(DB_FILE)
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
    )
    # Idempotent migrations for columns added after the initial schema.
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

def get_listing_row(con, lid):
    return con.execute("SELECT notified FROM listings WHERE id=?", (lid,)).fetchone()

def upsert_listing(con, lid, listing, notified=False):
    now = datetime.now().isoformat()
    source = listing.get("source", "jkk")
    address = listing.get("address") or listing.get("ward", "")
    if get_listing_row(con, lid):
        con.execute(
            "UPDATE listings SET last_seen=?, rent=?, source=?, address=?, disappeared_at=NULL,"
            " notified=MAX(notified,?) WHERE id=?",
            (now, listing["rent"], source, address, int(notified), lid),
        )
    else:
        con.execute(
            "INSERT INTO listings (id,name,ward,layout,rent,size_m2,url,first_seen,last_seen,notified,source,address) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, listing["name"], listing["ward"], listing["layout"],
             listing["rent"], listing["size_m2"], listing["url"], now, now,
             int(notified), source, address)
        )
    con.execute(
        "INSERT INTO snapshots (listing_id,rent,seen_at) VALUES (?,?,?)",
        (lid, listing["rent"], now)
    )
    con.commit()

def wait_stable(page, timeout=30_000):
    try:
        page.wait_for_load_state("load", timeout=timeout)
    except PWTimeout:
        pass
    for _ in range(15):
        try:
            page.wait_for_load_state("networkidle", timeout=3_000)
            return
        except PWTimeout:
            page.wait_for_timeout(1_000)
            log.debug(f"  waiting... {page.url}")

# ── Ward name lookup (for filtering by ward name) ─────────────────────────────
WARD_CODE = {
    "千代田区":"01","中央区":"02","港区":"03","新宿区":"04","文京区":"05",
    "台東区":"06","墨田区":"07","江東区":"08","品川区":"09","目黒区":"10",
    "大田区":"11","世田谷区":"12","渋谷区":"13","中野区":"14","杉並区":"15",
    "豊島区":"16","北区":"17","荒川区":"18","板橋区":"19","練馬区":"20",
    "足立区":"21","葛飾区":"22","江戸川区":"23",
}
CODE_WARD = {v: k for k, v in WARD_CODE.items()}

def fetch_listings():
    all_listings = []
    ok = False

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=["--disable-blink-features=AutomationControlled"]
                )
            except Exception:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"]
                )

            ctx = browser.new_context(
                locale="ja-JP",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"macOS"',
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                }
            )
            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

            # ── Step 1: get through the splash → condition search popup ──
            splash = ctx.new_page()
            log.info("Opening JKK splash page...")

            with ctx.expect_page(timeout=15_000) as popup_info:
                splash.goto(JKK_ENTRY, timeout=30_000)
                splash.wait_for_timeout(3_000)
                try:
                    fallback = splash.locator("a[href='#']").first
                    if fallback.count() > 0:
                        log.debug("Clicking fallback link")
                        fallback.click()
                except Exception:
                    pass

            popup = popup_info.value
            popup.wait_for_timeout(2_000)
            wait_stable(popup)
            log.info(f"Condition search page: {popup.url}")

            with open("jkk_debug_form.html", "w", encoding="utf-8") as f:
                f.write(popup.content())

            # ── Step 2: fill the form ──

            # Click "区部" master to enable/select all 23 wards
            log.info(f"Checking wards...")
            allku = popup.locator("input[name='akiyaInitRM.akiyaRefM.allCheck'][value='ALLKU']").first
            if allku.count() > 0 and not allku.is_checked():
                allku.check()
                popup.wait_for_timeout(500)

            # Click "市部" master to enable/select all 市
            allsi = popup.locator("input[name='akiyaInitRM.akiyaRefM.allCheck'][value='ALLSI']").first
            if allsi.count() > 0 and not allsi.is_checked():
                allsi.check()
                popup.wait_for_timeout(500)

            # Check all layout checkboxes
            log.info("Checking all layouts...")
            for val in ALL_LAYOUT_VALUES:
                cb = popup.locator(f"input[name='akiyaInitRM.akiyaRefM.madoris'][value='{val}']").first
                if cb.count() > 0 and not cb.is_checked():
                    cb.check()

            # ── Step 3: submit and collect results ──
            log.info("Submitting search form...")
            search_btn = popup.locator("a[onclick*='akiyaJyoukenRef']").first
            search_btn.click()
            wait_stable(popup)
            log.info(f"Results page: {popup.url}")

            with open("jkk_debug_results.html", "w", encoding="utf-8") as f:
                f.write(popup.content())
            log.debug("Results dumped → jkk_debug_results.html")

            # Check for no-results message
            results_html = popup.content()
            if "空室はございませんでした" in results_html:
                log.info("No vacancies found for current criteria.")
                browser.close()
                return all_listings

            # ── Step 4: paginate through all pages ──
            # First get total count from "N件が該当しました"
            import re as _re
            total_m = _re.search(r"(\d+)件が該当", popup.content())
            total_expected = int(total_m.group(1)) if total_m else 0
            log.info(f"Total expected: {total_expected} listings")

            page_num = 0
            while True:
                page_num += 1
                html = popup.content()

                if "owabi" in html or "おわび" in html:
                    log.warning("Got error page — stopping")
                    break

                new = parse_results(html, popup.url)
                all_listings.extend(new)
                log.info(f"Page {page_num}: {len(new)} listings (total: {len(all_listings)})")

                # Stop when we've collected all expected listings
                if total_expected > 0 and len(all_listings) >= total_expected:
                    log.info("Collected all listings.")
                    break
                if page_num >= 50:
                    log.warning("Hit 50-page safety cap.")
                    break

                next_btn = popup.locator("a[onclick*='afterPage']").first
                if next_btn.count() == 0 or not next_btn.is_visible():
                    log.info("No more pages.")
                    break

                next_btn.click()
                wait_stable(popup)

            browser.close()
        ok = True

    except PWTimeout as e:
        log.error(f"Playwright timeout: {e}")
    except Exception as e:
        log.error(f"Failed to fetch: {e}")

    log.info(f"Total listings fetched: {len(all_listings)}")
    for i, l in enumerate(all_listings):
        log.debug(
            f"  [{i+1:03}] {l['name']!r:40} | {l['ward']:5} | "
            f"¥{l['rent']:>7,} | {l['layout']:6} | {l['size_m2']}m²"
        )
    return all_listings, ok


def parse_results(html, current_url):
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for row in soup.select("table tr"):
        tds = row.find_all("td")
        # Real listing rows have exactly 11 tds and no th
        if len(tds) != 11 or row.find("th"):
            continue
        text = row.get_text(" ", strip=True)
        if not text:
            continue
        listing = extract_fields(tds, text, current_url)
        if listing:
            results.append(listing)
    return results


FWTABLE = str.maketrans(
    "１２３４５６７８９ＳＬＤＫＲＮＴＵＦＷＩＣｓｌｄｋ",
    "123456789SLDKRNTUFWICsldk"
)

def extract_fields(tds, text, base_url):
    # Column indices (from inspecting the results table):
    # [0]=image [1]=name [2]=ward [3]=priority [4]=type [5]=layout [6]=size [7]=rent [8]=kyoueki [9]=units [10]=detail
    if len(tds) < 10:
        return None

    name   = tds[1].get_text(strip=True)
    ward   = tds[2].get_text(strip=True)
    layout = tds[5].get_text(strip=True).translate(FWTABLE)
    size_text = tds[6].get_text(strip=True)
    rent_text = tds[7].get_text(strip=True)

    # Skip header rows
    if name in ("住宅名", "") or ward in ("地域", ""):
        return None

    # Rent — first number in the cell (handles ranges like 206,800～212,700)
    rent_raw = 0
    rent_m = re.search(r"(\d[\d,]+)", rent_text)
    if rent_m:
        rent_raw = int(rent_m.group(1).replace(",", ""))
    if rent_raw == 0:
        return None

    # Size — first number in cell
    size_m2 = 0.0
    size_m = re.search(r"([\d.]+)", size_text)
    if size_m:
        try:
            size_m2 = float(size_m.group(1))
        except ValueError:
            pass

    # URL from detail button in last td
    url = base_url
    for td in tds:
        a = td.find("a", href=True)
        if a and a.get("href","") not in ("", "#"):
            href = a["href"]
            url = href if href.startswith("http") else (JKK_BASE + href if href.startswith("/") else base_url)
            break

    return {
        "name": name, "address": ward, "ward": ward,
        "rent": rent_raw, "layout": layout, "size_m2": size_m2, "url": url,
        "source": "jkk",
    }


def matches_criteria(listing, config):
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
    return True


def get_maps_url(name, ward):
    query = requests.utils.quote(f"{name} {ward} 東京")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


SOURCE_META = {
    "jkk": {"emoji": "🏠", "label": "JKK", "footer": "JKK東京"},
    "ur":  {"emoji": "🏢", "label": "UR",  "footer": "UR都市機構"},
}


def send_slack(listing):
    """Send a Slack notification. Returns True if sent, False otherwise."""
    webhook = SLACK_WEBHOOK_URL or load_json(CONFIG_FILE, {}).get("slack_webhook", "")
    if not webhook:
        log.warning("SLACK_WEBHOOK_URL not set — skipping")
        return False

    meta = SOURCE_META.get(listing.get("source", "jkk"), SOURCE_META["jkk"])
    emoji, label, footer = meta["emoji"], meta["label"], meta["footer"]

    rent_str = f"¥{listing['rent']:,}" if listing["rent"] else "要確認"
    size_str = f"{listing['size_m2']} m²" if listing["size_m2"] else "—"

    maps_url = get_maps_url(listing["name"], listing["ward"])
    detail_url = listing.get("url") or maps_url
    actions = [
        {"type": "button",
         "text": {"type": "plain_text", "text": "🔗 物件ページ", "emoji": True},
         "url": detail_url,
         "style": "primary"},
        {"type": "button",
         "text": {"type": "plain_text", "text": "📍 Google Maps", "emoji": True},
         "url": maps_url},
    ]
    payload = {
        "text": f"{emoji} 新着{label}物件: {listing['name']} ({listing['ward']}) {rent_str}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text",
                                         "text": f"{emoji} 新着{label}物件が見つかりました！",
                                         "emoji": True}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*物件名* : {listing['name'] or '—'}"},
                {"type": "mrkdwn", "text": f"*所在地* :{listing['address'] or '—'}"},
                {"type": "mrkdwn", "text": f"*家賃* :{rent_str}"},
                {"type": "mrkdwn", "text": f"*間取り* :{listing['layout'] or '—'}"},
                {"type": "mrkdwn", "text": f"*専有面積* :{size_str}"},
            ]},
            {"type": "actions", "elements": actions},
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": f"Detected: {datetime.now().strftime('%Y-%m-%d %H:%M')} | {footer}"}
            ]}
        ]
    }

    try:
        r = requests.post(webhook, json=payload, timeout=10)
        r.raise_for_status()
        log.info(f"Slack sent ✓  [{label}] {listing['name']}")
        return True
    except Exception as e:
        log.error(f"Slack send failed: {e}")
        return False


def run():
    log.info("JKK + UR Monitor started ✓")
    con = init_db()
    log.info(f"Database: {DB_FILE}")

    while True:
        config = load_json(CONFIG_FILE, {})

        jkk_listings, jkk_ok = [], False
        ur_listings,  ur_ok  = [], False
        try:
            jkk_listings, jkk_ok = fetch_listings()
        except Exception as e:
            log.error(f"JKK fetch failed: {e}")
        try:
            ur_listings = fetch_ur_listings(config)
            ur_ok = True
        except Exception as e:
            log.error(f"UR fetch failed: {e}")

        listings = jkk_listings + ur_listings
        if not jkk_ok:
            log.warning("JKK fetch did not complete — skipping JKK disappearance detection this cycle.")
        if not ur_ok:
            log.warning("UR fetch did not complete — skipping UR disappearance detection this cycle.")

        new_count = {"jkk": 0, "ur": 0}
        for listing in listings:
            lid = listing_id(listing)
            row = get_listing_row(con, lid)
            already_notified = bool(row[0]) if row else False
            notified = False

            if not already_notified and matches_criteria(listing, config):
                rent_display = f"¥{listing['rent']:,}" if listing["rent"] else "不明"
                src = listing.get("source", "jkk").upper()
                log.info(f"NEW match [{src}] → {listing['name']} | {listing['ward']} | {rent_display}")
                notified = send_slack(listing)
                new_count[listing.get("source", "jkk")] = new_count.get(listing.get("source", "jkk"), 0) + 1

            upsert_listing(con, lid, listing, notified=notified)

        # Mark disappeared only for sources whose fetch completed successfully.
        # If JKK timed out but UR succeeded (or vice versa), we must not wipe
        # the failed source's listings — that would be a false disappearance.
        sources_ok = (["jkk"] if jkk_ok else []) + (["ur"] if ur_ok else [])
        if sources_ok:
            now_iso = datetime.now().isoformat()
            cutoff = (datetime.now() - timedelta(seconds=int(POLL_INTERVAL * 1.5))).isoformat()
            placeholders = ",".join("?" * len(sources_ok))
            cur = con.execute(
                f"UPDATE listings SET disappeared_at=? "
                f"WHERE disappeared_at IS NULL AND last_seen < ? AND source IN ({placeholders})",
                [now_iso, cutoff] + sources_ok,
            )
            con.commit()
            if cur.rowcount:
                log.info(f"{cur.rowcount} listing(s) marked as disappeared.")

        total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        log.info(f"Cycle done — {new_count['jkk']} new JKK | {new_count['ur']} new UR | "
                 f"{total} total in DB. Next check in {POLL_INTERVAL // 60}m.")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
