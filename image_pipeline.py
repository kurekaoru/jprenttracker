"""
Shared image pipeline utilities used by all scrapers.

Each scraper is responsible for producing a list of (url, image_type) tuples.
This module handles the common steps: deduplication upsert, download, OCR,
thumbnail selection, and floor plan analysis.
"""

import os, json, logging, base64, requests, sqlite3
from datetime import datetime
from urllib.parse import urljoin
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

IMG_DIR = "images"

_IMG_HEADERS = {"User-Agent": "jkktrackr/1.0 (image-collector)"}

_DETAIL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

SCRAPER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Lower priority number = preferred thumbnail
THUMB_PRIORITY = {
    "exterior":   0,
    "interior":   1,
    "facilities": 2,
    "floor_plan": 3,
    "appliances": 4,
}

_FLOOR_PLAN_KEYWORDS = {"madori", "floorplan", "floor_plan", "floor-plan", "間取", "madorizu"}
_SKIP_KEYWORDS       = {"logo", "icon", "btn", "arrow", "spacer", "blank", "banner", "nophoto"}


# ── Utilities ──────────────────────────────────────────────────────────────────

def reset_asyncio_loop():
    """Clear any stale/running asyncio loop before calling sync_playwright."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def ocr_floor_plan(image_path: str) -> str | None:
    """
    Ask Claude Haiku to extract room dimensions from an image.
    Returns a text summary, or None if the image is not a floor plan.
    """
    import os as _os
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        with open(image_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        ext  = image_path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png",  "webp": "image/webp"}.get(ext, "image/jpeg")
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                {"type": "text", "text": (
                    "This is a Japanese rental apartment floor plan (間取り図). "
                    "List every room and its size in m². Format each line as: "
                    "Room: Xm²  (e.g. LDK: 14.5m², 洋室1: 6.0m², 洗面所: 2.5m²). "
                    "If this image is NOT a floor plan, reply only: NOT_FLOOR_PLAN"
                )},
            ]}],
        )
        text = resp.content[0].text.strip()
        return None if text.startswith("NOT_FLOOR_PLAN") else text
    except Exception as e:
        log.warning(f"OCR failed for {image_path}: {e}")
        return None


def run_floor_plan_analysis(lid: str, con: sqlite3.Connection) -> None:
    """Run Claude vision floor plan analysis and store result in listings.floor_plan_data."""
    fp_row = con.execute(
        "SELECT local_path FROM listing_images "
        "WHERE listing_id=? AND image_type='floor_plan' AND local_path IS NOT NULL "
        "ORDER BY id LIMIT 1",
        (lid,),
    ).fetchone()
    if not fp_row or not os.path.exists(fp_row[0]):
        return
    try:
        from floor_plan_agent import FloorPlanAgent
        size_row = con.execute("SELECT size_m2 FROM listings WHERE id=?", (lid,)).fetchone()
        agent  = FloorPlanAgent()
        result = agent.analyze(fp_row[0], total_area_m2=size_row[0] if size_row else None)
        con.execute(
            "UPDATE listings SET floor_plan_data=? WHERE id=?",
            (json.dumps(result.to_dict(), ensure_ascii=False), lid),
        )
        con.commit()
        log.info(f"Floor plan analysed for {lid}: {len(result.rooms)} rooms, "
                 f"living={result.living_area_m2}m²")
    except Exception as e:
        log.warning(f"Floor plan analysis failed for {lid}: {e}")


def _merge_appliances(lid: str, new_items: list[str], con: sqlite3.Connection) -> None:
    """Union new appliance items into listings.appliances (comma-separated, deduped)."""
    row = con.execute("SELECT appliances FROM listings WHERE id=?", (lid,)).fetchone()
    existing = set(i.strip() for i in (row[0] or "").split(",") if i.strip()) if row else set()
    merged = existing | set(new_items)
    con.execute("UPDATE listings SET appliances=? WHERE id=?",
                (", ".join(sorted(merged)), lid))
    con.commit()


def download_image(url: str, row_id: int, lid: str, img_type: str,
                   con: sqlite3.Connection) -> str | None:
    """
    Download one image, run OCR if it's a floor plan candidate.
    Updates listing_images with local_path + ocr_text.
    Returns local file path on success, None on failure.
    """
    if not url or url.startswith("file://"):
        return None
    img_dir = os.path.join(IMG_DIR, lid)
    os.makedirs(img_dir, exist_ok=True)
    try:
        r = requests.get(url, timeout=15, headers=_IMG_HEADERS)
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or not ct.startswith("image"):
            log.debug(f"Skip non-image {url} ({r.status_code} {ct})")
            return None
        ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
        if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
            ext = "jpg"
        fpath = os.path.join(img_dir, f"{row_id}.{ext}")
        with open(fpath, "wb") as f:
            f.write(r.content)
        # AI classification overrides the URL heuristic
        ai_type = classify_image_ai(fpath)
        if ai_type == "skip":
            os.remove(fpath)
            con.execute("DELETE FROM listing_images WHERE id=?", (row_id,))
            con.commit()
            log.debug(f"Skipped junk image {url}")
            return None
        if ai_type != img_type:
            log.debug(f"Reclassified {url}: {img_type} → {ai_type}")
            img_type = ai_type
        con.execute("UPDATE listing_images SET image_type=? WHERE id=?", (img_type, row_id))

        ocr_text = None
        if img_type == "floor_plan":
            ocr_text = ocr_floor_plan(fpath)
            if ocr_text:
                log.info(f"OCR floor plan {fpath}: {ocr_text[:80]}")
        elif img_type == "appliances":
            items = extract_appliances(fpath)
            if items:
                ocr_text = ", ".join(items)
                log.info(f"Appliances found: {ocr_text}")
                _merge_appliances(lid, items, con)

        con.execute(
            "UPDATE listing_images SET local_path=?, downloaded_at=?, ocr_text=? WHERE id=?",
            (fpath, datetime.now().isoformat(), ocr_text, row_id),
        )
        log.debug(f"Saved {fpath}")
        return fpath
    except Exception as e:
        log.warning(f"Image download failed ({url}): {e}")
        return None


def save_images_for_listing(lid: str, images: list[tuple[str, str]],
                             con: sqlite3.Connection) -> int:
    """
    Given [(url, image_type), …], upsert records, download files, pick the
    best thumbnail, and run floor plan analysis.

    Returns the number of successfully downloaded images.
    This is the shared step-2–5 pipeline; scrapers supply step 1 (the URL list).
    """
    images = [(u, t) for u, t in images if u and u.startswith("http")]
    if not images:
        return 0

    # 1. Upsert URL records
    for img_url, img_type in images:
        con.execute(
            "INSERT OR IGNORE INTO listing_images (listing_id, url, image_type) VALUES (?,?,?)",
            (lid, img_url, img_type),
        )
    con.commit()

    # 2. Download + thumbnail tracking
    best_thumb: tuple | None = None  # (priority, url)
    ok = 0

    for img_url, img_type in images:
        row = con.execute(
            "SELECT id, local_path FROM listing_images WHERE listing_id=? AND url=?",
            (lid, img_url),
        ).fetchone()
        if not row:
            continue
        row_id, existing_path = row

        if existing_path and os.path.exists(existing_path):
            prio = THUMB_PRIORITY.get(img_type, 99)
            if best_thumb is None or prio < best_thumb[0]:
                best_thumb = (prio, img_url)
            ok += 1
            continue

        fpath = download_image(img_url, row_id, lid, img_type, con)
        if fpath:
            ok += 1
            # Re-read type — OCR may have reclassified floor_plan → interior/exterior
            actual_type = con.execute(
                "SELECT image_type FROM listing_images WHERE id=?", (row_id,)
            ).fetchone()[0]
            prio = THUMB_PRIORITY.get(actual_type, 99)
            if best_thumb is None or prio < best_thumb[0]:
                best_thumb = (prio, img_url)

    if best_thumb:
        con.execute("UPDATE listings SET thumbnail_url=? WHERE id=?", (best_thumb[1], lid))
    con.commit()

    # 3. Floor plan analysis
    if ok > 0:
        run_floor_plan_analysis(lid, con)

    return ok


def classify_image_heuristic(url: str, alt: str = "", parent_text: str = "") -> str:
    """Cheap URL/alt heuristic — used as a pre-download hint only."""
    combined = (url + " " + alt + " " + parent_text).lower()
    if any(k in combined for k in _FLOOR_PLAN_KEYWORDS):
        return "floor_plan"
    if any(k in combined for k in ("gaikan", "外観", "exterior")):
        return "exterior"
    return "interior"


_VALID_TYPES = frozenset(("floor_plan", "interior", "exterior", "facilities", "appliances", "skip"))

def classify_image_ai(image_path: str) -> str:
    """
    Use Claude Haiku to classify a downloaded image.

    Returns one of:
      floor_plan  — 間取り図: room layout diagram
      exterior    — building exterior / aerial / street view
      interior    — inside the unit (rooms, kitchen, bathroom, hallway)
      facilities  — shared building facilities (lobby, mail room, parking, pool)
      appliances  — close-up of equipment/appliances (AC unit, interphone, etc.)
      skip        — banner, logo, transport map, icon, or site decoration

    Falls back to 'interior' on API failure.
    """
    import os as _os
    try:
        from anthropic import Anthropic
    except ImportError:
        return "interior"
    api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "interior"
    try:
        with open(image_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        ext  = image_path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png",  "webp": "image/webp",
                "gif": "image/gif"}.get(ext, "image/jpeg")
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                {"type": "text", "text": (
                    "Classify this image from a Japanese apartment rental listing. "
                    "Reply with exactly one word from this list:\n"
                    "floor_plan — 間取り図: room layout diagram with labels/dimensions\n"
                    "exterior — building exterior, aerial, or street-view photo\n"
                    "interior — photo inside the unit (rooms, kitchen, bathroom, hallway)\n"
                    "facilities — shared building spaces (lobby, corridor, parking, laundry room, pool)\n"
                    "appliances — close-up of equipment or appliances "
                    "(air conditioning unit, interphone, video doorbell, stove, bath unit, etc.)\n"
                    "skip — banner, logo, transport diagram, icon, map, or site decoration"
                )},
            ]}],
        )
        label = resp.content[0].text.strip().lower().split()[0]
        return label if label in _VALID_TYPES else "interior"
    except Exception as e:
        log.warning(f"AI classify failed for {image_path}: {e}")
        return "interior"


def extract_appliances(image_path: str) -> list[str]:
    """
    For an image already classified as 'appliances', identify what is visible.
    Returns a list of appliance/equipment names in English, e.g.
    ["air conditioning", "video interphone", "bathroom ventilation fan"].
    Returns [] on failure or if nothing recognisable.
    """
    import os as _os
    try:
        from anthropic import Anthropic
    except ImportError:
        return []
    api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []
    try:
        with open(image_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        ext  = image_path.rsplit(".", 1)[-1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png",  "webp": "image/webp",
                "gif": "image/gif"}.get(ext, "image/jpeg")
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                {"type": "text", "text": (
                    "List the appliances or equipment visible in this Japanese apartment image. "
                    "Use short English names separated by commas "
                    "(e.g. 'air conditioning, video interphone, IH stove'). "
                    "If nothing recognisable, reply: none"
                )},
            ]}],
        )
        text = resp.content[0].text.strip()
        if text.lower() == "none":
            return []
        return [item.strip() for item in text.split(",") if item.strip()]
    except Exception as e:
        log.warning(f"Appliance extraction failed for {image_path}: {e}")
        return []


def fetch_generic_images(listing_url: str) -> list[tuple[str, str]]:
    """
    Scrape image URLs from a plain-HTML listing page.
    Returns [(url, image_type), …] capped at 12, or [] on failure.
    Suitable for sites that don't require JS rendering or session cookies.
    """
    if not listing_url or listing_url.startswith("javascript"):
        return []
    try:
        r = requests.get(listing_url, timeout=15, headers=_DETAIL_HEADERS)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.content, "html.parser")
        seen, results = set(), []

        def add(url, img_type):
            if url and url not in seen and not any(k in url.lower() for k in _SKIP_KEYWORDS):
                ext = url.rsplit(".", 1)[-1].split("?")[0].lower()
                if ext in ("jpg", "jpeg", "png", "webp", "gif") or "?" in url:
                    seen.add(url)
                    results.append((url, img_type))

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            add(og["content"], "exterior")

        for img in soup.find_all("img", src=True):
            src = urljoin(listing_url, img["src"])
            alt = img.get("alt", "")
            parent = " ".join(
                cls for p in img.parents
                if hasattr(p, "get")
                for cls in p.get("class", [])
            )
            add(src, classify_image(src, alt, parent))

        results.sort(key=lambda x: 0 if x[1] == "exterior" else 1 if x[1] == "interior" else 2)
        return results[:12]
    except Exception as e:
        log.debug(f"Generic image fetch failed ({listing_url}): {e}")
    return []


def download_pending_images(con: sqlite3.Connection, limit: int = 50) -> None:
    """
    Catch-up downloader: retry images whose URL is known but local file is missing.
    Does not run floor plan analysis (handled upstream by each scraper's pipeline).
    """
    rows = con.execute(
        "SELECT id, listing_id, url, image_type, local_path FROM listing_images "
        "WHERE url NOT LIKE 'file://%' ORDER BY id LIMIT ?",
        (limit * 4,),
    ).fetchall()
    to_retry = [(r[0], r[1], r[2], r[3]) for r in rows
                if not r[4] or not os.path.exists(r[4])][:limit]
    if not to_retry:
        return
    log.info(f"Re-downloading {len(to_retry)} missing image(s)...")
    for row_id, lid, url, img_type in to_retry:
        download_image(url, row_id, lid, img_type, con)
        con.commit()
