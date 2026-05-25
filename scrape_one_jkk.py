"""
One-shot: open a live JKK session and directly probe mz_copyright images
for a specific listing by its mz_id.

Usage: python3 scrape_one_jkk.py [listing_id]
Default target: 落合 多摩市 3DK (74518b2a)
"""

import os, re, sys, sqlite3, logging
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from scraper_jkk import JKKScraper, JKK_BASE
from image_pipeline import (
    IMG_DIR, classify_image_ai, ocr_floor_plan, run_floor_plan_analysis,
    extract_appliances, THUMB_PRIORITY, _merge_appliances,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

DB_FILE = "jkk_monitor.db"
TARGET  = sys.argv[1] if len(sys.argv) > 1 else "74518b2a1878216f161b75d6db3b10bd"


def probe_mz_in_session(ctx, lid: str, mz_id: str, con: sqlite3.Connection) -> int:
    """Probe mz_copyright sequences 000-029 using active session cookies."""
    img_dir = os.path.join(IMG_DIR, lid)
    os.makedirs(img_dir, exist_ok=True)

    # Remove any stale placeholder records
    con.execute("DELETE FROM listing_images WHERE listing_id=? AND local_path IS NULL", (lid,))
    con.commit()

    saved = 0
    best_thumb: tuple | None = None

    for seq in range(1, 31):
        url = f"{JKK_BASE}/mz_copyright/mobile/{mz_id}/{mz_id}{seq:03d}.jpg"
        try:
            resp = ctx.request.get(url, timeout=10_000)
            if resp.status != 200:
                log.info(f"  seq {seq:03d}: HTTP {resp.status} — stopping")
                break
            ct = resp.headers.get("content-type", "")
            if not ct.startswith("image"):
                log.info(f"  seq {seq:03d}: non-image content-type {ct} — stopping")
                break
            body = resp.body()

            con.execute(
                "INSERT OR IGNORE INTO listing_images (listing_id, url, image_type) VALUES (?,?,?)",
                (lid, url, "interior"),
            )
            con.commit()
            row_id = con.execute(
                "SELECT id FROM listing_images WHERE listing_id=? AND url=?", (lid, url)
            ).fetchone()[0]

            fpath = os.path.join(img_dir, f"{row_id}.jpg")
            with open(fpath, "wb") as f:
                f.write(body)

            img_type = classify_image_ai(fpath)
            log.info(f"  seq {seq:03d}: {img_type} → {os.path.basename(fpath)}")

            if img_type == "skip":
                os.remove(fpath)
                con.execute("DELETE FROM listing_images WHERE id=?", (row_id,))
                con.commit()
                continue

            ocr_text = None
            if img_type == "floor_plan":
                ocr_text = ocr_floor_plan(fpath)
                log.info(f"    OCR: {ocr_text[:100] if ocr_text else 'NOT_FLOOR_PLAN → reclassify exterior'}")
                if not ocr_text:
                    img_type = "exterior"
            elif img_type == "appliances":
                items = extract_appliances(fpath)
                if items:
                    ocr_text = ", ".join(items)
                    log.info(f"    appliances: {ocr_text}")
                    _merge_appliances(lid, items, con)

            con.execute(
                "UPDATE listing_images SET image_type=?, local_path=?, downloaded_at=?, ocr_text=? WHERE id=?",
                (img_type, fpath, datetime.now().isoformat(), ocr_text, row_id),
            )
            con.commit()

            prio = THUMB_PRIORITY.get(img_type, 99)
            if best_thumb is None or prio < best_thumb[0]:
                best_thumb = (prio, f"/api/images/{row_id}")
            saved += 1

        except Exception as e:
            log.warning(f"  seq {seq:03d}: failed — {e}")
            break

    if best_thumb:
        con.execute("UPDATE listings SET thumbnail_url=? WHERE id=?", (best_thumb[1], lid))
        con.commit()
        log.info(f"  thumbnail set → {best_thumb[1]}")

    return saved


def run():
    con = sqlite3.connect(DB_FILE, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")

    row = con.execute(
        "SELECT id, name, thumbnail_url, priority, building_type FROM listings WHERE id=?",
        (TARGET,),
    ).fetchone()
    if not row:
        log.error(f"Listing {TARGET} not found")
        return
    lid, name, thumb, priority, building_type = row

    # Extract mz_id from stored thumbnail URL
    m = re.search(r"mz_copyright/mobile/(\d+)/", thumb or "")
    if not m:
        log.error(f"No mz_copyright URL found for {name}. thumbnail={thumb}")
        return
    mz_id = m.group(1)

    log.info(f"Target:  {name} ({lid[:8]})")
    log.info(f"mz_id:   {mz_id}")
    log.info(f"priority: {priority}  |  building_type: {building_type}")

    # Open a JKK Playwright session
    from playwright.sync_api import sync_playwright
    from scraper_jkk import JKKScraper

    scraper = JKKScraper(DB_FILE)
    log.info("Opening JKK session…")

    with sync_playwright() as p:
        browser = scraper._launch_browser(p)
        ctx = browser.new_context(
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        from scraper_jkk import JKK_ENTRY
        splash = ctx.new_page()
        log.info("Navigating to JKK splash…")
        with ctx.expect_page(timeout=15_000) as popup_info:
            splash.goto(JKK_ENTRY, timeout=30_000)
            splash.wait_for_timeout(3_000)
            try:
                fallback = splash.locator("a[href='#']").first
                if fallback.count() > 0:
                    fallback.click()
            except Exception:
                pass

        popup = popup_info.value
        popup.wait_for_timeout(2_000)
        scraper._wait_stable(popup)
        log.info(f"Session established: {popup.url}")

        # Session cookies are now live — probe mz_copyright
        log.info(f"Probing mz_copyright sequences for mz_id={mz_id}…")
        n = probe_mz_in_session(ctx, lid, mz_id, con)
        browser.close()

    log.info(f"\n{n} image(s) saved")
    if n > 0:
        run_floor_plan_analysis(lid, con)

    # Final summary
    imgs = con.execute(
        "SELECT id, image_type, local_path, ocr_text FROM listing_images WHERE listing_id=? ORDER BY id",
        (lid,),
    ).fetchall()
    meta = con.execute(
        "SELECT thumbnail_url, floor_plan_data, appliances FROM listings WHERE id=?", (lid,)
    ).fetchone()

    log.info(f"\n{'─'*60}")
    log.info(f"  {name}")
    log.info(f"  priority: {priority}  |  type: {building_type}")
    log.info(f"  thumbnail:  {meta[0]}")
    log.info(f"  floor_plan: {'✓ ' + meta[1][:120] if meta[1] else '✗ none'}")
    log.info(f"  appliances: {meta[2] or 'none'}")
    log.info(f"  images ({len(imgs)}):")
    for img in imgs:
        iid, itype, path, ocr = img
        has = "✓" if path and os.path.exists(path) else "✗"
        preview = f"  ocr={ocr[:50]}" if ocr else ""
        log.info(f"    [{iid}] {(itype or '?'):12} {has}  {os.path.basename(path or '')} {preview}")

    con.close()


if __name__ == "__main__":
    run()
