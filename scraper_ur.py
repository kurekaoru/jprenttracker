"""
UR都市機構 (Urban Renaissance Agency) Housing Monitor — Tokyo + Kanagawa.

API flow per ward:
  1. POST .../bukken/result/bukken_result/ with mode=area + skcs=<ward_code>
     → list of danchi (housing complexes) with vacancies in that ward,
       paginated 10 per page.
  2. For each danchi, POST .../bukken/result/bukken_result_room/ with the
     same body plus shisya/danchi/shikibetu identifiers
     → list of currently-vacant rooms in that complex.

Images are fetched via bukken/detail/detail_room/ API (same backend the UR website
calls via AJAX). URL pattern: /{shisya}_{danchi}{shikibetu}_room.html?JKSS={id}
"""

import re, time, random, sqlite3, logging, json
import html as html_lib
import requests

from scraper_base import BaseScraper
from image_pipeline import save_images_for_listing

log = logging.getLogger(__name__)

UR_API_BASE        = "https://chintai.r6.ur-net.go.jp/chintai/api/"
UR_BUKKEN_RESULT   = UR_API_BASE + "bukken/result/bukken_result/"
UR_BUKKEN_ROOM     = UR_API_BASE + "bukken/result/bukken_result_room/"
UR_BUKKEN_DETAIL   = UR_API_BASE + "bukken/detail/detail_room/"
UR_WEB_BASE      = "https://www.ur-net.go.jp"

# Each region is (tdfk, block, {ward_name: skcs})
UR_REGIONS = [
    # ── Tokyo (tdfk=13) ───────────────────────────────────────────────────────
    ("13", "kanto", {
        # 23 special wards
        "千代田区": "101", "中央区": "102", "港区":   "103", "新宿区": "104",
        "文京区":   "105", "台東区": "106", "墨田区": "107", "江東区": "108",
        "品川区":   "109", "目黒区": "110", "大田区": "111", "世田谷区":"112",
        "渋谷区":   "113", "中野区": "114", "杉並区": "115", "豊島区": "116",
        "北区":     "117", "荒川区": "118", "板橋区": "119", "練馬区": "120",
        "足立区":   "121", "葛飾区": "122", "江戸川区":"123",
        # Tama-area cities
        "八王子市": "201", "立川市":   "202", "武蔵野市": "203", "三鷹市":   "204",
        "府中市":   "206", "昭島市":   "207", "調布市":   "208", "町田市":   "209",
        "小金井市": "210", "小平市":   "211", "日野市":   "212", "東村山市": "213",
        "国分寺市": "214", "国立市":   "215", "福生市":   "218", "狛江市":   "219",
        "清瀬市":   "221", "東久留米市":"222", "武蔵村山市":"223", "多摩市":   "224",
        "稲城市":   "225", "羽村市":   "227", "西東京市": "229",
    }),
    # ── Kanagawa (tdfk=14) ───────────────────────────────────────────────────
    ("14", "kanto", {
        # Yokohama wards
        "横浜市鶴見区":    "101", "横浜市神奈川区": "102", "横浜市西区":   "103",
        "横浜市中区":      "104", "横浜市南区":     "105", "横浜市保土ケ谷区":"106",
        "横浜市磯子区":    "107", "横浜市金沢区":   "108", "横浜市港北区": "109",
        "横浜市戸塚区":    "110", "横浜市港南区":   "111", "横浜市旭区":   "112",
        "横浜市緑区":      "113", "横浜市瀬谷区":   "114", "横浜市栄区":   "115",
        "横浜市青葉区":    "117", "横浜市都筑区":   "118",
        # Kawasaki wards
        "川崎市川崎区":    "131", "川崎市幸区":     "132", "川崎市中原区": "133",
        "川崎市高津区":    "134", "川崎市麻生区":   "135", "川崎市多摩区": "136",
        "川崎市宮前区":    "137",
        # Sagamihara
        "相模原市緑区":    "151", "相模原市中央区": "152", "相模原市南区": "153",
    }),
]

# Build a pattern to extract the primary municipality from a UR address string.
# Some danchi straddle ward/city boundaries; the UR API assigns them to whichever
# search code (skcs) surfaced them, which can mismatch the actual address.
# We match the first known municipality that appears in the address and use that.
_ALL_WARDS = sorted(
    (ward for _, _, codes in UR_REGIONS for ward in codes),
    key=len, reverse=True,  # longest first so e.g. 世田谷区 beats 田区
)
_WARD_RE = re.compile("|".join(re.escape(w) for w in _ALL_WARDS))

def _ward_from_address(address, fallback):
    if address:
        m = _WARD_RE.search(address)
        if m:
            return m.group(0)
    return fallback


_HEADERS = {
    "Origin": "https://www.ur-net.go.jp",
    "Referer": "https://www.ur-net.go.jp/",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _base_body(skcs, tdfk, block, page_index=0, shisya="", danchi="", shikibetu=""):
    return {
        "rent_low": "", "rent_high": "",
        "walk": "",
        "floorspace_low": "", "floorspace_high": "",
        "years": "",
        "mode": "area",
        "skcs": skcs,
        "block": block,
        "tdfk": tdfk,
        "rireki_tdfk": tdfk,
        "orderByField": "1",
        "pageSize": "10",
        "pageIndex": str(page_index),
        "shisya": shisya, "danchi": danchi, "shikibetu": shikibetu,
        "pageIndexRoom": "0",
        "sp": "",
    }


def _post(url, body, timeout=30, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=body, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < retries:
                import time as _t
                _t.sleep(2 * (attempt + 1))
    raise last_err


def _fetch_danchi_for_ward(skcs, tdfk, block):
    out = []
    page = 0
    while page < 50:
        data = _post(UR_BUKKEN_RESULT, _base_body(skcs, tdfk, block, page_index=page))
        if not data:
            break
        out.extend(data)
        try:
            page_max = int(data[0].get("pageMax", "1"))
        except (ValueError, TypeError):
            page_max = 1
        if page + 1 >= page_max:
            break
        page += 1
    return out


def _fetch_rooms_for_danchi(skcs, tdfk, block, danchi_obj):
    body = _base_body(
        skcs, tdfk, block,
        shisya=danchi_obj.get("shisya", ""),
        danchi=danchi_obj.get("danchi", ""),
        shikibetu=danchi_obj.get("shikibetu", ""),
    )
    return _post(UR_BUKKEN_ROOM, body) or []


_RENT_RE = re.compile(r"([\d,]+)")
_NUM_RE  = re.compile(r"([\d.]+)")

def _normalize_layout(raw):
    """Normalize UR layout strings to match the dashboard LAYOUTS list."""
    s = (raw or "").strip()
    s = re.sub(r"S$", "", s)          # drop service-room suffix: 2DKS→2DK, 2LDKS→2LDK
    if re.match(r"^[4-9]", s):        # 4LDK, 4K, 4DK, etc. → 4LDK以上
        s = "4LDK以上"
    return s or raw


def _parse_room(room, danchi, ward_name):
    rent_m = _RENT_RE.search(room.get("rent") or "")
    rent = int(rent_m.group(1).replace(",", "")) if rent_m else 0
    if rent == 0:
        return None

    fs_text = html_lib.unescape(room.get("floorspace") or "")
    fs_m = _NUM_RE.search(fs_text)
    size_m2 = float(fs_m.group(1)) if fs_m else 0.0

    name = (danchi.get("danchiNm") or "").strip()
    label = f"{room.get('roomNmMain','')}{room.get('roomNmSub','')}".strip()
    if label:
        name = f"{name} {label}" if name else label

    link = room.get("roomLinkPc") or ""
    url = (UR_WEB_BASE + link) if link.startswith("/") else (link or UR_WEB_BASE)

    # thumbnail_url left None — headless scraper will populate listing_images
    thumbnail_url = None

    address = (danchi.get("place") or "").strip()
    return {
        "name":          name or "(無名)",
        "address":       address,
        "ward":          _ward_from_address(address, ward_name),
        "rent":          rent,
        "layout":        _normalize_layout(room.get("type") or ""),
        "size_m2":       size_m2,
        "url":           url,
        "source":        "ur",
        "thumbnail_url": thumbnail_url,
    }


def _fetch_all_ur_listings():
    """Pull all current UR vacancies. Returns list of listing dicts."""
    listings = []
    for tdfk, block, area_codes in UR_REGIONS:
        region_label = "東京" if tdfk == "13" else "神奈川"
        log.info(f"UR: fetching {region_label} ({len(area_codes)} area(s))...")
        for ward_name, skcs in area_codes.items():
            try:
                danchi_list = _fetch_danchi_for_ward(skcs, tdfk, block)
            except Exception as e:
                log.error(f"UR: {ward_name} ({skcs}) danchi fetch failed: {e}")
                continue
            if not danchi_list:
                continue
            active    = [d for d in danchi_list if str(d.get("roomCount", "0")) not in ("0", "")]
            ward_count = 0
            for d in active:
                try:
                    rooms = _fetch_rooms_for_danchi(skcs, tdfk, block, d)
                except Exception as e:
                    log.error(f"UR: rooms fetch failed for {d.get('danchiNm','?')}: {e}")
                    continue
                for r in rooms:
                    parsed = _parse_room(r, d, ward_name)
                    if parsed:
                        listings.append(parsed)
                        ward_count += 1
            if ward_count:
                log.info(f"UR: {ward_name} ({skcs}) → {ward_count} room(s) across "
                         f"{len(active)}/{len(danchi_list)} complex(es) with vacancies")
    log.info(f"UR: total {len(listings)} listing(s) fetched.")
    return listings


# Keep old name for any external callers
def fetch_ur_listings(config):
    return _fetch_all_ur_listings()


# ── UR image scraping ─────────────────────────────────────────────────────────

def _classify_ur_image(src: str, alt: str) -> str | None:
    """Return image_type for a UR image, or None to skip."""
    if '間取' in alt or 'madori' in src.lower():
        return 'floor_plan'
    if '交通図' in alt or '_TF_' in src or '_photo_s' in src:
        return None  # transport diagram or thumbnail-only images
    if '外観' in alt or '共用施設' in alt:
        return 'exterior'
    return 'interior'


def _parse_ur_url_params(listing_url: str) -> tuple[str, str, str, str] | None:
    """
    Extract (shisya, danchi, shikibetu, room_id) from a UR room page URL.
    URL pattern: /{shisya}_{danchi}{shikibetu}_room.html?JKSS={id}
    e.g. /20_4130_room.html?JKSS=000070404 → ('20','413','0','000070404')
    """
    m = re.search(r'/(\d+)_(\d+)_room\.html.*JKSS=([A-Za-z0-9]+)', listing_url)
    if not m:
        return None
    shisya = m.group(1)
    danchi_shiki = m.group(2)   # e.g. "4130"
    danchi   = danchi_shiki[:-1]  # "413"
    shikibetu = danchi_shiki[-1]  # "0"
    room_id = m.group(3)
    return shisya, danchi, shikibetu, room_id


_HTML_TAG_RE  = re.compile(r'<[^>]+>')
_PARKING_FEE  = re.compile(r'料金[：:]\s*([\d,]+)円')
_PARKING_NONE = re.compile(r'なし|空きなし|利用不可')

def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub('', html_lib.unescape(text or '')).strip()

def _parse_parking(raw: str) -> tuple[int, int]:
    """Return (has_parking, fee_jpy) from parking field text."""
    if not raw:
        return 0, 0
    clean = _strip_html(raw)
    if _PARKING_NONE.search(clean):
        return 0, 0
    fee = 0
    m = _PARKING_FEE.search(clean)
    if m:
        fee = int(m.group(1).replace(',', ''))
    return 1, fee


def _fetch_ur_room_data(listing_url: str) -> tuple[list[tuple[str, str]], dict]:
    """
    Call bukken/detail/detail_room/ and return (images, detail_dict).

    images  — [(url, image_type), …] sorted exterior-first, floor_plan-last
    detail  — structured dict with keys: features, facility, parking,
              age_years, available_date, feature_text, notes, shikikin
    Returns ([], {}) on failure.
    """
    params = _parse_ur_url_params(listing_url)
    if not params:
        log.debug(f"UR: could not parse URL params from {listing_url}")
        return [], {}
    shisya, danchi, shikibetu, room_id = params
    try:
        resp = requests.post(
            UR_BUKKEN_DETAIL,
            data={"id": room_id, "shisya": shisya, "danchi": danchi,
                  "shikibetu": shikibetu, "sp": ""},
            headers=_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if not data or not isinstance(data, list):
            return [], {}
        d = data[0]

        # ── images ──────────────────────────────────────────────────────────
        seen, images = set(), []
        for img in (d.get("img") or []):
            url = img.get("画像パス") or ""
            alt = img.get("ALT") or ""
            if not url or not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            img_type = _classify_ur_image(url, alt)
            if img_type is None:
                continue
            images.append((url, img_type))
        images.sort(key=lambda x: 2 if x[1] == "floor_plan" else 0 if x[1] == "exterior" else 1)

        # ── detail ──────────────────────────────────────────────────────────
        has_parking, parking_fee = _parse_parking(d.get("parking") or "")
        raw_year = d.get("year") or ""
        try:
            age_years = int(raw_year)
        except (ValueError, TypeError):
            age_years = None
        detail = {
            "features":       d.get("feature_pickup") or [],   # list of highlight tags
            "facility":       _strip_html(d.get("facility") or ""),
            "parking":        _strip_html(d.get("parking") or ""),
            "has_parking":    has_parking,
            "parking_fee":    parking_fee,
            "age_years":      age_years,
            "available_date": d.get("availableDate") or "",
            "feature_text":   _strip_html(d.get("feature") or ""),
            "notes":          _strip_html(d.get("biko") or ""),
            "shikikin":       d.get("shikikin") or "",
        }
        return images[:20], detail

    except Exception as e:
        log.debug(f"UR detail_room API failed ({listing_url}): {e}")
        return [], {}


# Keep old name for callers that don't need detail
def _fetch_ur_image_urls(listing_url: str) -> list[tuple[str, str]]:
    images, _ = _fetch_ur_room_data(listing_url)
    return images


def _save_ur_detail(lid: str, detail: dict, con: sqlite3.Connection) -> None:
    """Persist structured UR room detail to the listings row."""
    if not detail:
        return
    features_csv = ", ".join(detail["features"]) if detail["features"] else None
    # Build a human-readable detail_text from all fields
    parts = []
    if detail["features"]:
        parts.append("特徴ピックアップ\n" + "\n".join(detail["features"]))
    if detail["facility"]:
        parts.append("設備\n" + detail["facility"])
    if detail["parking"]:
        parts.append("敷地内駐車場\n" + detail["parking"])
    if detail["age_years"] is not None:
        parts.append(f"管理年数\n{detail['age_years']}年")
    if detail["available_date"]:
        parts.append(f"入居可能時期\n{detail['available_date']}")
    if detail["feature_text"]:
        parts.append("特徴\n" + detail["feature_text"])
    if detail["notes"]:
        parts.append("備考\n" + detail["notes"])
    detail_text = "\n\n".join(parts) if parts else None

    con.execute(
        "UPDATE listings SET "
        "  features=COALESCE(?, features),"
        "  appliances=COALESCE(?, appliances),"
        "  detail_text=COALESCE(?, detail_text),"
        "  has_parking=COALESCE(?, has_parking),"
        "  parking_fee=COALESCE(?, parking_fee),"
        "  age_years=COALESCE(?, age_years),"
        "  available_from=COALESCE(?, available_from),"
        "  nearby_features=COALESCE(?, nearby_features)"
        " WHERE id=?",
        (
            features_csv,
            detail["facility"] or None,
            detail_text,
            detail["has_parking"] if detail.get("has_parking") is not None else None,
            detail["parking_fee"] if detail.get("parking_fee") else None,
            detail["age_years"],
            detail["available_date"] or None,
            detail["feature_text"] or None,
            lid,
        ),
    )
    con.commit()


# ── URScraper class ───────────────────────────────────────────────────────────

class URScraper(BaseScraper):
    source = "ur"

    def fetch_listings(self, config: dict) -> tuple[list[dict], bool]:
        try:
            return _fetch_all_ur_listings(), True
        except Exception as e:
            log.error(f"UR fetch failed: {e}")
            return [], False

    def fetch_images_batch(self, new_lids: list[str]) -> int:
        """Fetch images for new UR listings via the detail_room API."""
        if not new_lids:
            return 0
        con = sqlite3.connect(self.db_file, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT id, url FROM listings WHERE id IN ({','.join('?'*len(new_lids))})",
            new_lids,
        ).fetchall()
        ur_rows = [(r["id"], r["url"]) for r in rows if "ur-net.go.jp" in (r["url"] or "")]
        if not ur_rows:
            con.close()
            return 0

        ok = 0
        try:
            for lid, listing_url in ur_rows:
                try:
                    images, detail = _fetch_ur_room_data(listing_url)
                    n = save_images_for_listing(lid, images, con)
                    _save_ur_detail(lid, detail, con)
                    if n:
                        ok += 1
                        log.info(f"UR images: {n} saved for {lid[:8]}")
                except Exception as e:
                    log.error(f"UR image pipeline failed ({listing_url}): {e}")
                time.sleep(random.uniform(1, 3))
        except Exception as e:
            log.error(f"UR image batch failed: {e}")
        finally:
            con.close()
        return ok

    def backfill_images(self, listing_ids: list[str] | None = None,
                        limit: int = 50) -> None:
        """Re-scrape images for UR listings with missing image data."""
        con = sqlite3.connect(self.db_file, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        con.row_factory = sqlite3.Row

        # Clean unrecoverable file:// records
        deleted = con.execute("DELETE FROM listing_images WHERE url LIKE 'file://%'").rowcount
        con.execute("UPDATE listings SET thumbnail_url=NULL WHERE thumbnail_url LIKE 'file://%'")
        con.commit()
        if deleted:
            log.info(f"Backfill: removed {deleted} unrecoverable file:// record(s)")

        if listing_ids:
            placeholders = ",".join("?" * len(listing_ids))
            rows = con.execute(
                f"SELECT id, url FROM listings "
                f"WHERE id IN ({placeholders}) AND disappeared_at IS NULL",
                listing_ids,
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT DISTINCT l.id, l.url FROM listings l
                   WHERE l.disappeared_at IS NULL AND l.source = 'ur'
                     AND (
                       NOT EXISTS (SELECT 1 FROM listing_images li WHERE li.listing_id = l.id)
                       OR
                       NOT EXISTS (SELECT 1 FROM listing_images li
                                   WHERE li.listing_id = l.id AND li.local_path IS NOT NULL
                                     AND li.local_path != '')
                     )
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        if not rows:
            log.info("Backfill: nothing to process")
            con.close()
            return

        log.info(f"Backfill: processing {len(rows)} listing(s)…")
        ok = 0
        try:
            for row in rows:
                lid, url = row["id"], row["url"]
                if not url or "ur-net.go.jp" not in url:
                    continue
                try:
                    images, detail = _fetch_ur_room_data(url)
                    n = save_images_for_listing(lid, images, con)
                    _save_ur_detail(lid, detail, con)
                    if n:
                        ok += 1
                        log.info(f"  ✓ {lid[:8]} — {n} image(s)")
                    else:
                        log.warning(f"  ✗ {lid[:8]} — no images from {url}")
                except Exception as e:
                    log.error(f"  ✗ {lid[:8]} failed: {e}")
                time.sleep(random.uniform(1, 3))
            log.info(f"Backfill done: {ok}/{len(rows)} listings updated")
        except Exception as e:
            log.error(f"Backfill failed: {e}")
        finally:
            con.close()


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ward = sys.argv[1] if len(sys.argv) > 1 else "横浜市港北区"
    rows = _fetch_all_ur_listings()
    filtered = [r for r in rows if r["ward"] == ward]
    print(json.dumps(filtered, ensure_ascii=False, indent=2))
    print(f"\n{len(filtered)} listings in {ward} (total: {len(rows)})")
