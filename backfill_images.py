"""
Backfill full images (floor plans + interiors + exterior) for all active listings.

UR  — uses headless Playwright to render each room page (JS-heavy site).
      Reuses a single browser instance across all listings for speed.
JKK — images require live session cookies; runs a fresh JKK scrape which now
      automatically downloads images in-session via _save_jkk_images_in_session.

Resumes safely: skips listings that already have a floor_plan image.

Usage:
    python backfill_images.py [--ur-only] [--jkk-only] [--limit N] [--delay F]
"""

import argparse, logging, sqlite3, time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def backfill_ur(limit: int, delay: float):
    from playwright.sync_api import sync_playwright
    from scraper4 import (
        DB_FILE, IMG_DIR, listing_id,
        _CHROME_ARGS, _SCRAPER_UA,
        _fetch_ur_images_playwright,
        download_pending_images,
    )
    import os

    con = sqlite3.connect(DB_FILE, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """
        SELECT id, url FROM listings
        WHERE source = 'ur'
          AND disappeared_at IS NULL
          AND url IS NOT NULL
          AND id NOT IN (
              SELECT DISTINCT listing_id FROM listing_images
              WHERE image_type = 'floor_plan'
          )
        ORDER BY first_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        log.info("UR: nothing to backfill — all active listings already have floor plans.")
        con.close()
        return

    log.info(f"UR: backfilling {len(rows)} listing(s) with headless browser…")

    # Share a single browser but create a fresh page per listing.
    # Reusing the same page causes UR's JS to not re-initialize image galleries.
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_CHROME_ARGS)

        scraped = 0
        for i, row in enumerate(rows, 1):
            lid, url = row["id"], row["url"]
            log.info(f"[{i}/{len(rows)}] {lid}  {url[:80]}")

            # Fresh page each time so UR's image gallery JS initialises cleanly
            page = browser.new_page(user_agent=_SCRAPER_UA)
            try:
                images = _fetch_ur_images_playwright(url, _page=page)
            finally:
                page.close()

            if not images:
                log.warning("  No images found")
                time.sleep(delay)
                continue

            fp_count = sum(1 for _, t in images if t == "floor_plan")
            log.info(f"  {len(images)} image(s) ({fp_count} floor plan(s))")

            # Only update thumbnail if it isn't already set
            existing_thumb = con.execute(
                "SELECT thumbnail_url FROM listings WHERE id=?", (lid,)
            ).fetchone()
            if not existing_thumb or not existing_thumb[0]:
                con.execute(
                    "UPDATE listings SET thumbnail_url=? WHERE id=?",
                    (images[0][0], lid),
                )
            for img_url, img_type in images:
                con.execute(
                    "INSERT OR IGNORE INTO listing_images (listing_id, url, image_type) VALUES (?,?,?)",
                    (lid, img_url, img_type),
                )
            con.commit()
            scraped += 1
            time.sleep(delay)

        browser.close()

    log.info(f"UR: queued images for {scraped}/{len(rows)} listing(s). Downloading…")

    # Download + OCR floor plans
    rounds = 0
    while True:
        pending = con.execute(
            "SELECT COUNT(*) FROM listing_images WHERE local_path IS NULL"
        ).fetchone()[0]
        if not pending:
            break
        log.info(f"  {pending} image(s) pending…")
        download_pending_images(con, limit=20)
        rounds += 1
        if rounds > 200:
            break
        time.sleep(0.2)

    con.close()
    log.info("UR backfill done.")


def backfill_jkk():
    """
    JKK images require live session cookies.  Running fetch_listings() triggers
    _save_jkk_images_in_session which downloads everything in-session.
    """
    from scraper4 import fetch_listings, download_pending_images, DB_FILE
    import sqlite3

    log.info("JKK: running fresh scrape to collect images in-session…")
    listings, ok = fetch_listings()
    if not ok:
        log.error("JKK scrape did not complete successfully.")
        return

    log.info(f"JKK: scrape done ({len(listings)} listing(s)). Downloading remaining…")
    con = sqlite3.connect(DB_FILE, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    rounds = 0
    while True:
        pending = con.execute(
            "SELECT COUNT(*) FROM listing_images WHERE local_path IS NULL"
        ).fetchone()[0]
        if not pending:
            break
        log.info(f"  {pending} image(s) pending…")
        download_pending_images(con, limit=20)
        rounds += 1
        if rounds > 100:
            break
        time.sleep(0.2)
    con.close()
    log.info("JKK backfill done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ur-only",  action="store_true")
    ap.add_argument("--jkk-only", action="store_true")
    ap.add_argument("--limit",  type=int,   default=500, help="Max UR listings (default: all)")
    ap.add_argument("--delay",  type=float, default=2.0, help="Seconds between UR page loads")
    args = ap.parse_args()

    run_ur  = not args.jkk_only
    run_jkk = not args.ur_only

    if run_ur:
        backfill_ur(args.limit, args.delay)
    if run_jkk:
        backfill_jkk()

    log.info("All done.")
