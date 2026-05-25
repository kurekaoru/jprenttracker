"""
One-shot: open a live JKK session, navigate to a specific listing's detail page,
download ALL images found there, and classify them.

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


def collect_detail_images(ctx, page, lid: str, con: sqlite3.Connection) -> int:
    """Extract all images from the current detail page and classify them."""
    img_dir = os.path.join(IMG_DIR, lid)
    os.makedirs(img_dir, exist_ok=True)

    # Remove stale placeholder records
    con.execute("DELETE FROM listing_images WHERE listing_id=? AND local_path IS NULL", (lid,))
    con.commit()

    html = page.content()
    with open("/tmp/jkk_detail_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    log.info("  Saved detail page HTML to /tmp/jkk_detail_debug.html")

    # Find all img src values on the page
    srcs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    log.info(f"  Found {len(srcs)} img tags on detail page")

    # Also look for any URL containing mz_copyright or .jpg/.png patterns in scripts/data
    extra = re.findall(
        r'https?://[^\s"\'<>]+(?:\.jpg|\.jpeg|\.png|\.gif)',
        html, re.IGNORECASE
    )
    all_srcs = list(dict.fromkeys(srcs + extra))  # deduplicate, preserve order
    log.info(f"  {len(all_srcs)} unique image URLs (including inline)")

    saved = 0
    best_thumb: tuple | None = None

    for src in all_srcs:
        url = src if src.startswith("http") else (
            JKK_BASE + src if src.startswith("/") else None
        )
        if not url:
            continue

        # Skip tiny icons, spacers, style images
        if any(x in url for x in ["/styles/", "/images/btn_", "/common/", "spacer", "logo",
                                   "btn_", "ico_", "icon", "arrow", "header", "footer", "nav"]):
            log.debug(f"  Skipping UI asset: {url}")
            continue

        ext = url.rsplit(".", 1)[-1].lower()
        if ext not in ("jpg", "jpeg", "png", "gif"):
            continue

        try:
            resp = ctx.request.get(url, timeout=10_000)
            if resp.status != 200:
                log.debug(f"  {url}: HTTP {resp.status}")
                continue
            ct = resp.headers.get("content-type", "")
            if not ct.startswith("image"):
                log.debug(f"  {url}: non-image content-type {ct}")
                continue
            body = resp.body()
            if len(body) < 2048:
                log.debug(f"  {url}: too small ({len(body)} bytes) — skipping")
                continue

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
            log.info(f"  {img_type:12} ← {os.path.basename(url)}")

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
            log.warning(f"  Failed {url}: {e}")

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

    # Extract mz_id from stored thumbnail URL (raw or /api/images/ form)
    mz_id = None
    m = re.search(r"mz_copyright/mobile/(\d+)/", thumb or "")
    if m:
        mz_id = m.group(1)
    else:
        # Try to get mz_id from listing_images table
        img_row = con.execute(
            "SELECT url FROM listing_images WHERE listing_id=? AND url LIKE '%mz_copyright%' LIMIT 1",
            (lid,)
        ).fetchone()
        if img_row:
            m2 = re.search(r"mz_copyright/mobile/(\d+)/", img_row[0])
            if m2:
                mz_id = m2.group(1)

    log.info(f"Target:   {name} ({lid[:8]})")
    log.info(f"mz_id:    {mz_id}")
    log.info(f"priority: {priority}  |  building_type: {building_type}")

    scraper = JKKScraper(DB_FILE)
    log.info("Opening JKK session…")

    from playwright.sync_api import sync_playwright
    from scraper_jkk import JKK_ENTRY

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

        # Submit search form to get to results page
        for value in ["ALLKU", "ALLSI"]:
            cb = popup.locator(
                f"input[name='akiyaInitRM.akiyaRefM.allCheck'][value='{value}']"
            ).first
            if cb.count() > 0 and not cb.is_checked():
                cb.check()
                popup.wait_for_timeout(300)
        for val in ["1","2","3","4"]:
            cb = popup.locator(
                f"input[name='akiyaInitRM.akiyaRefM.madoris'][value='{val}']"
            ).first
            if cb.count() > 0 and not cb.is_checked():
                cb.check()

        search_btn = popup.locator("a[onclick*='akiyaJyoukenRef']").first
        search_btn.click()
        scraper._wait_stable(popup)
        log.info(f"Results page loaded: {popup.url}")

        # Find the listing row by mz_id in onclick, paginate if needed
        detail_found = False
        for page_num in range(50):
            html = popup.content()
            # Look for senPage or href containing mz_id
            if mz_id and mz_id in html:
                log.info(f"Found mz_id={mz_id} on page {page_num+1}")
                # Click the row's link/onclick
                locator = popup.locator(f"[onclick*='{mz_id}']").first
                if locator.count() > 0:
                    log.info("Clicking listing row…")
                    locator.click()
                    scraper._wait_stable(popup)
                    log.info(f"Detail page: {popup.url}")
                    detail_found = True
                    break
                else:
                    # Try finding by name text as fallback
                    locator2 = popup.locator(f"text={name}").first
                    if locator2.count() > 0:
                        locator2.click()
                        scraper._wait_stable(popup)
                        detail_found = True
                        break

            next_btn = popup.locator("a[onclick*='afterPage']").first
            if next_btn.count() == 0 or not next_btn.is_visible():
                log.warning("Reached last page without finding listing.")
                break
            next_btn.click(timeout=60_000)
            scraper._wait_stable(popup)

        if not detail_found:
            # Fallback: if mz_id not found via onclick, try to scrape images directly
            log.warning("Could not navigate to detail page — falling back to mz_copyright probe (seq 001-030)")
            from scrape_one_jkk import probe_mz_in_session  # type: ignore
            n = probe_mz_in_session(ctx, lid, mz_id, con) if mz_id else 0
            browser.close()
            log.info(f"\n{n} image(s) saved (fallback)")
            if n > 0:
                run_floor_plan_analysis(lid, con)
        else:
            n = collect_detail_images(ctx, popup, lid, con)
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


def probe_mz_in_session(ctx, lid: str, mz_id: str, con: sqlite3.Connection) -> int:
    """Fallback: probe mz_copyright sequences 001-030 using active session cookies."""
    img_dir = os.path.join(IMG_DIR, lid)
    os.makedirs(img_dir, exist_ok=True)

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
                if not ocr_text:
                    img_type = "exterior"
            elif img_type == "appliances":
                items = extract_appliances(fpath)
                if items:
                    ocr_text = ", ".join(items)
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

    return saved


if __name__ == "__main__":
    run()
