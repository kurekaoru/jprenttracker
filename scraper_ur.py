"""
UR都市機構 (Urban Renaissance Agency) Tokyo Housing Monitor.

Talks to the UR JSON API directly — no headless browser needed.

API flow per ward:
  1. POST .../bukken/result/bukken_result/ with mode=area + skcs=<ward_code>
     → list of danchi (housing complexes) with vacancies in that ward,
       paginated 10 per page.
  2. For each danchi, POST .../bukken/result/bukken_result_room/ with the
     same body plus shisya/danchi/shikibetu identifiers
     → list of currently-vacant rooms in that complex.

Each room becomes one listing dict, shaped like JKK listings so that
scraper3.run() can process JKK and UR results uniformly.
"""

import re
import html as html_lib
import logging
import requests

log = logging.getLogger(__name__)

UR_API_BASE       = "https://chintai.r6.ur-net.go.jp/chintai/api/"
UR_BUKKEN_RESULT  = UR_API_BASE + "bukken/result/bukken_result/"
UR_BUKKEN_ROOM    = UR_API_BASE + "bukken/result/bukken_result_room/"
UR_WEB_BASE       = "https://www.ur-net.go.jp"

UR_TOKYO_TDFK  = "13"
UR_TOKYO_BLOCK = "kanto"

# Tokyo ward / city → UR area code (skcs). Extracted from
# https://www.ur-net.go.jp/chintai/kanto/tokyo/
UR_WARD_CODE = {
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
}

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


def _base_body(skcs, page_index=0, shisya="", danchi="", shikibetu=""):
    # Mirrors what frmMain serializes in the browser. Empty strings for
    # filters mean "no filter" — we let the harness apply rent/layout
    # filters in matches_criteria(), the same way JKK does.
    return {
        "rent_low": "", "rent_high": "",
        "walk": "",
        "floorspace_low": "", "floorspace_high": "",
        "years": "",
        "mode": "area",
        "skcs": skcs,
        "block": UR_TOKYO_BLOCK,
        "tdfk": UR_TOKYO_TDFK,
        "rireki_tdfk": UR_TOKYO_TDFK,
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


def _fetch_danchi_for_ward(skcs):
    """Return all danchi (housing complexes) with vacancies in a ward."""
    out = []
    page = 0
    while page < 50:
        data = _post(UR_BUKKEN_RESULT, _base_body(skcs, page_index=page))
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


def _fetch_rooms_for_danchi(skcs, danchi_obj):
    body = _base_body(
        skcs,
        shisya=danchi_obj.get("shisya", ""),
        danchi=danchi_obj.get("danchi", ""),
        shikibetu=danchi_obj.get("shikibetu", ""),
    )
    return _post(UR_BUKKEN_ROOM, body) or []


_RENT_RE = re.compile(r"([\d,]+)")
_NUM_RE  = re.compile(r"([\d.]+)")


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

    return {
        "name":    name or "(無名)",
        "address": (danchi.get("place") or "").strip(),
        "ward":    ward_name,
        "rent":    rent,
        "layout":  (room.get("type") or "").strip(),
        "size_m2": size_m2,
        "url":     url,
        "source":  "ur",
    }


def fetch_ur_listings(config):
    """
    Pull all current Tokyo UR vacancies, optionally restricted to wards
    listed in config["wards"]. Returns a list of listing dicts shaped
    like JKK listings so scraper3.run() can consume them uniformly.
    """
    wards = list(UR_WARD_CODE.items())

    log.info(f"UR: fetching {len(wards)} ward(s)...")
    listings = []
    for ward_name, skcs in wards:
        try:
            danchi_list = _fetch_danchi_for_ward(skcs)
        except Exception as e:
            log.error(f"UR: ward {ward_name} ({skcs}) danchi fetch failed: {e}")
            continue
        if not danchi_list:
            continue

        # bukken_result returns every danchi in the ward, vacancies or not.
        # roomCount > 0 means there's at least one currently-vacant room in
        # that complex — skip the rest to avoid useless room-detail calls.
        active = [d for d in danchi_list if str(d.get("roomCount", "0")) not in ("0", "")]
        ward_count = 0
        for d in active:
            try:
                rooms = _fetch_rooms_for_danchi(skcs, d)
            except Exception as e:
                log.error(f"UR: rooms fetch failed for {d.get('danchiNm','?')}: {e}")
                continue
            for r in rooms:
                parsed = _parse_room(r, d, ward_name)
                if parsed:
                    listings.append(parsed)
                    ward_count += 1
        log.info(f"UR: {ward_name} ({skcs}) → {ward_count} room(s) across "
                 f"{len(active)}/{len(danchi_list)} complex(es) with vacancies")

    log.info(f"UR: total {len(listings)} listing(s) fetched.")
    return listings


if __name__ == "__main__":
    # Smoke test: list current UR vacancies for a single ward.
    import json, sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = {"wards": ["江東区"]} if len(sys.argv) < 2 else {"wards": [sys.argv[1]]}
    rows = fetch_ur_listings(cfg)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
