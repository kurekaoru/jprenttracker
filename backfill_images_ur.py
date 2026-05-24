"""
Backfill images + floor-plan OCR for existing UR listings.

Resumes safely: skips any listing_id already in listing_images.
Run from the project directory with the venv active:
    python backfill_images_ur.py [--limit N] [--delay 0.8]
"""

import argparse, logging, sqlite3, sys, time
from scraper4 import (
    DB_FILE,
    _fetch_detail_images,
    download_pending_images,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def backfill(limit: int, delay: float):
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        """
        SELECT id, url FROM listings
        WHERE source = 'ur'
          AND disappeared_at IS NULL
          AND url IS NOT NULL
          AND id NOT IN (SELECT DISTINCT listing_id FROM listing_images)
        ORDER BY first_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        log.info("Nothing to backfill — all UR listings already have images.")
        return

    log.info(f"Backfilling images for {len(rows)} UR listing(s)…")

    scraped = 0
    for i, (lid, url) in enumerate(rows, 1):
        log.info(f"[{i}/{len(rows)}] {lid}  {url[:80]}")
        images = _fetch_detail_images(url)
        if not images:
            log.warning(f"  No images found")
            time.sleep(delay)
            continue

        fp_count = sum(1 for _, t in images if t == "floor_plan")
        log.info(f"  {len(images)} image(s) found ({fp_count} floor plan(s))")

        # Set thumbnail from first image
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

    log.info(f"Scraped image URLs for {scraped}/{len(rows)} listing(s). Downloading…")

    # Download all queued images (+ OCR floor plans)
    # Run in batches of 20 until nothing left
    rounds = 0
    while True:
        pending = con.execute(
            "SELECT COUNT(*) FROM listing_images WHERE local_path IS NULL"
        ).fetchone()[0]
        if not pending:
            break
        log.info(f"  {pending} image(s) pending download…")
        download_pending_images(con, limit=20)
        rounds += 1
        if rounds > 50:  # safety cap
            break
        time.sleep(0.3)

    con.close()
    log.info("Done.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="Max listings to process (default: all)")
    ap.add_argument("--delay", type=float, default=0.8,
                    help="Seconds between listing fetches (default: 0.8)")
    args = ap.parse_args()
    backfill(args.limit, args.delay)
