"""
One-shot: re-classify all existing JKK images using AI vision, fix thumbnails,
delete junk, extract appliances, re-run floor plan analysis.

Run on VM: python3 reclassify_jkk_images.py
"""

import os, json, sqlite3, logging, sys
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from image_pipeline import (
    classify_image_ai, extract_appliances, run_floor_plan_analysis,
    THUMB_PRIORITY, _merge_appliances,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

DB_FILE = "jkk_monitor.db"


def reclassify():
    con = sqlite3.connect(DB_FILE, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")

    rows = con.execute("""
        SELECT li.id, li.listing_id, li.local_path, li.image_type, l.name
        FROM listing_images li
        JOIN listings l ON l.id = li.listing_id
        WHERE l.source = 'jkk'
          AND li.local_path IS NOT NULL
        ORDER BY l.name, li.id
    """).fetchall()

    log.info(f"Reclassifying {len(rows)} JKK images…")

    # Group by listing so we can update thumbnail + floor plan once per listing
    by_listing: dict[str, list] = {}
    for img_id, lid, path, old_type, name in rows:
        by_listing.setdefault(lid, []).append((img_id, path, old_type, name))

    total_skipped = 0
    total_changed = 0

    for lid, images in by_listing.items():
        listing_name = images[0][3]
        log.info(f"\n── {listing_name} ({lid[:8]}) — {len(images)} image(s)")

        best_thumb: tuple | None = None  # (priority, url)

        for img_id, path, old_type, _ in images:
            if not os.path.exists(path):
                log.warning(f"  Missing file: {path}")
                continue

            new_type = classify_image_ai(path)
            log.info(f"  [{img_id}] {os.path.basename(path)}  {old_type} → {new_type}")

            if new_type == "skip":
                os.remove(path)
                con.execute("DELETE FROM listing_images WHERE id=?", (img_id,))
                con.commit()
                total_skipped += 1
                continue

            # Update type in DB
            con.execute("UPDATE listing_images SET image_type=? WHERE id=?", (new_type, img_id))
            con.commit()

            if new_type != old_type:
                total_changed += 1

            # Appliance annotation
            if new_type == "appliances":
                items = extract_appliances(path)
                if items:
                    log.info(f"    appliances: {', '.join(items)}")
                    ocr_text = ", ".join(items)
                    con.execute("UPDATE listing_images SET ocr_text=? WHERE id=?", (ocr_text, img_id))
                    _merge_appliances(lid, items, con)

            # Track best thumbnail candidate
            url_row = con.execute("SELECT url FROM listing_images WHERE id=?", (img_id,)).fetchone()
            if url_row:
                prio = THUMB_PRIORITY.get(new_type, 99)
                if best_thumb is None or prio < best_thumb[0]:
                    best_thumb = (prio, url_row[0])

        # Update thumbnail for this listing
        if best_thumb:
            # Convert raw mz_copyright URL to /api/images/<id> form used by the server
            url_to_id = dict(con.execute(
                "SELECT url, id FROM listing_images WHERE listing_id=?", (lid,)
            ).fetchall())
            img_id_for_thumb = url_to_id.get(best_thumb[1])
            thumb = f"/api/images/{img_id_for_thumb}" if img_id_for_thumb else best_thumb[1]
            con.execute("UPDATE listings SET thumbnail_url=? WHERE id=?", (thumb, lid))
            con.commit()
            log.info(f"  thumbnail → {thumb}")

        # Re-run floor plan analysis if any floor_plan image exists
        run_floor_plan_analysis(lid, con)

    log.info(f"\nDone — {total_skipped} deleted (junk), {total_changed} reclassified.")
    con.close()


if __name__ == "__main__":
    reclassify()
