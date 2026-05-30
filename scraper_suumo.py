"""
SUUMO rental scraper for Tokyo wards.

Scrapes https://suumo.jp/chintai/tokyo/sc_{ward}/ using plain HTTP requests.
No Playwright required — the listing pages are server-rendered HTML.

Each SUUMO page has ~20 buildings, each with 1–N rooms.
Each room becomes a separate listing row in the DB.
"""

import re, time, random, sqlite3, hashlib, logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime

from scraper_base import BaseScraper

log = logging.getLogger(__name__)

SUUMO_BASE = "https://suumo.jp"

# Ward slug → (display_name, prefecture)
# Add more wards here to expand coverage.
SUUMO_WARDS = {
    "nakano":       ("中野区",   "東京都"),
    # "shinjuku":   ("新宿区",   "東京都"),
    # "shibuya":    ("渋谷区",   "東京都"),
    # "setagaya":   ("世田谷区", "東京都"),
    # "suginami":   ("杉並区",   "東京都"),
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Referer": "https://suumo.jp/",
}

_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)


def _get(url: str, retries: int = 3) -> requests.Response | None:
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, timeout=20)
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f"SUUMO GET failed ({attempt+1}/{retries}): {url} — {e}")
            time.sleep(2 ** attempt)
    return None


def _parse_man_yen(text: str | None) -> int | None:
    """'7.4万円' → 74000, '15万円' → 150000, None if missing/dash."""
    if not text:
        return None
    m = re.search(r"([\d.]+)万円", text)
    if m:
        return int(float(m.group(1)) * 10_000)
    m2 = re.search(r"(\d+)円", text)
    if m2:
        return int(m2.group(1))
    return None


def _parse_age(text: str | None) -> float | None:
    """'築11年' → 11.0, '新築' → 0.0."""
    if not text:
        return None
    if "新築" in text:
        return 0.0
    m = re.search(r"築(\d+)年", text)
    return float(m.group(1)) if m else None


def _parse_station(text: str) -> tuple[str | None, int | None]:
    """'西武新宿線/鷺ノ宮駅 歩4分' → ('鷺ノ宮駅（西武新宿線）', 4)"""
    m = re.match(r"(.+?)/(.+?駅)\s*歩(\d+)分", text)
    if m:
        line, station, minutes = m.group(1), m.group(2), m.group(3)
        return f"{station}（{line}）", int(minutes)
    return None, None


def _extract_ward(address: str) -> str | None:
    """'東京都中野区鷺宮３' → '中野区'."""
    m = re.search(r"([^\s都道府県]+?[区市町村])", address)
    return m.group(1) if m else None


def _extract_pref(address: str) -> str | None:
    """'東京都中野区...' → '東京都'."""
    m = re.match(r"(東京都|大阪府|北海道|.+?[都道府県])", address)
    return m.group(1) if m else None


def _scrape_ward_page(ward_slug: str, page: int) -> list[dict]:
    """Scrape one results page and return list of room-level listing dicts."""
    url = f"{SUUMO_BASE}/chintai/tokyo/sc_{ward_slug}/"
    if page > 1:
        url += f"?page={page}"

    r = _get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    listings = []

    for bld in soup.select(".cassetteitem"):
        # — Building-level fields —
        btype_el = bld.select_one(".cassetteitem_content-label span")
        building_type = btype_el.text.strip() if btype_el else None

        addr_el = bld.select_one(".cassetteitem_detail-col1")
        address = addr_el.text.strip() if addr_el else ""
        ward = _extract_ward(address)
        prefecture = _extract_pref(address)

        station_els = bld.select(".cassetteitem_detail-col2 .cassetteitem_detail-text")
        nearest_station, walk_min = None, None
        if station_els:
            nearest_station, walk_min = _parse_station(station_els[0].text.strip())

        age_divs = bld.select(".cassetteitem_detail-col3 div")
        age_text = age_divs[0].text.strip() if age_divs else None
        age_years = _parse_age(age_text)

        # Thumbnail image for the building (first in building-level img)
        bld_img_el = bld.select_one(".cassetteitem_object img[rel]")
        building_thumb = bld_img_el["rel"] if bld_img_el else None

        # — Room rows —
        for row in bld.select("tbody tr.js-cassette_link"):
            tds = row.select("td")
            if len(tds) < 6:
                continue

            floor_text = tds[2].text.strip()
            rent_raw   = tds[3].text.strip()
            dep_raw    = tds[4].text.strip()
            lay_raw    = tds[5].text.strip()

            rent = _parse_man_yen(rent_raw)
            if not rent:
                continue  # skip rows with no rent

            admin_fee_m = re.search(r"([\d.]+)万円|(\d+)円", rent_raw.split()[-1] if rent_raw else "")
            admin_fee = None
            if admin_fee_m:
                v = admin_fee_m.group(1) or admin_fee_m.group(2)
                admin_fee = int(float(v) * 10_000) if admin_fee_m.group(1) else int(v)

            dep_parts = re.findall(r"([\d.]+)万円|-", dep_raw)
            deposit   = int(float(dep_parts[0]) * 10_000) if dep_parts and dep_parts[0] not in ("-", "") else 0
            key_money = int(float(dep_parts[1]) * 10_000) if len(dep_parts) > 1 and dep_parts[1] not in ("-", "") else 0

            lay_m = re.match(r"(\S+)\s+([\d.]+)m", lay_raw)
            layout  = lay_m.group(1) if lay_m else lay_raw.split()[0] if lay_raw else None
            size_m2 = float(lay_m.group(2)) if lay_m else None

            # Room detail URL (unique per room)
            link_el = row.select_one("a[href*='/chintai/']")
            if not link_el:
                continue
            room_path = link_el["href"]
            room_url  = SUUMO_BASE + room_path
            room_id_m = re.search(r"/(jnc_\w+)/", room_path)
            if not room_id_m:
                continue
            suumo_id = room_id_m.group(1)

            # Room images from data-imgs (comma-separated)
            img_el = row.select_one("[data-imgs]")
            image_urls = img_el["data-imgs"].split(",") if img_el else []
            if not image_urls and building_thumb:
                image_urls = [building_thumb]

            listings.append({
                "suumo_id":      suumo_id,
                "name":          f"{address} {layout}",
                "address":       address,
                "ward":          ward,
                "prefecture":    prefecture,
                "rent":          rent,
                "admin_fee":     admin_fee,
                "deposit":       deposit,
                "key_money":     key_money,
                "layout":        layout,
                "size_m2":       size_m2,
                "floor":         floor_text,
                "building_type": building_type,
                "age_years":     age_years,
                "nearest_station": nearest_station,
                "walk_min":      walk_min,
                "url":           room_url,
                "source":        "suumo",
                "thumbnail_url": image_urls[0] if image_urls else None,
                "image_urls":    image_urls,
            })

    return listings


def _last_page(ward_slug: str) -> int:
    """Get the total number of pages for a ward."""
    r = _get(f"{SUUMO_BASE}/chintai/tokyo/sc_{ward_slug}/")
    if not r:
        return 1
    soup = BeautifulSoup(r.text, "html.parser")
    pager = soup.select_one(".pagination-parts")
    if not pager:
        return 1
    page_links = pager.select("a")
    if not page_links:
        return 1
    try:
        return int(page_links[-1].text.strip())
    except ValueError:
        return 1


def _listing_id(listing: dict) -> str:
    """Mirror of scraper4.listing_id() — must stay in sync."""
    src = listing.get("source", "suumo")
    key = f"{src}-{listing.get('name','')}-{listing.get('address','')}-{listing.get('layout','')}-{listing.get('rent','')}"
    return hashlib.md5(key.encode()).hexdigest()


class SuumoScraper(BaseScraper):
    source = "suumo"

    # Max pages per ward per cycle. Set to None to scrape all pages.
    # 20 listings/page × 50 pages = 1000 listings max per ward per cycle.
    MAX_PAGES = 50

    def __init__(self, db_file: str):
        super().__init__(db_file)
        # lid (MD5) → list of image URLs; populated in fetch_listings, drained in fetch_images_batch
        self._pending_images: dict[str, list[str]] = {}

    def fetch_listings(self, config: dict) -> tuple[list[dict], bool]:
        all_listings = []
        ok = True
        self._pending_images.clear()

        wards = dict(SUUMO_WARDS)

        for ward_slug, (ward_name, prefecture) in wards.items():
            try:
                last = _last_page(ward_slug)
                max_p = self.MAX_PAGES or last
                pages_to_fetch = min(last, max_p)
                log.info(f"SUUMO {ward_name}: {last} pages total, fetching {pages_to_fetch}")

                for page in range(1, pages_to_fetch + 1):
                    rows = _scrape_ward_page(ward_slug, page)
                    for r in rows:
                        lid = _listing_id(r)
                        if r.get("image_urls"):
                            self._pending_images[lid] = r["image_urls"]
                    all_listings.extend(rows)
                    log.debug(f"SUUMO {ward_name} p{page}: {len(rows)} rooms")
                    if page < pages_to_fetch:
                        time.sleep(random.uniform(0.8, 1.5))

            except Exception as e:
                log.error(f"SUUMO {ward_name} fetch error: {e}", exc_info=True)
                ok = False

        log.info(f"SUUMO total: {len(all_listings)} room listings")
        return all_listings, ok

    def fetch_images_batch(self, new_lids: list[str]) -> int:
        """Bulk-insert all image URLs collected during fetch_listings."""
        if not new_lids:
            return 0

        con = sqlite3.connect(self.db_file, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")

        saved = 0
        for lid in new_lids:
            urls = self._pending_images.get(lid, [])
            if not urls:
                continue
            for url in urls:
                con.execute(
                    "INSERT OR IGNORE INTO listing_images (listing_id, url) VALUES (?,?)",
                    (lid, url),
                )
            saved += 1

        con.commit()
        con.close()
        return saved
