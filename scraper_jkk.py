"""
JKK Tokyo Housing Monitor scraper.

Session flow:
  Splash → popup (condition search form) → check all wards → submit → results → paginate

Images are fetched during the same Playwright session using mz_copyright URLs
(session cookies are required — these return 403 without them).
"""

import os, re, json, logging, sqlite3
from datetime import datetime

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from scraper_base import BaseScraper
from image_pipeline import (
    IMG_DIR, ocr_floor_plan, run_floor_plan_analysis,
    THUMB_PRIORITY, classify_image_ai,
)

log = logging.getLogger(__name__)

JKK_BASE  = "https://jhomes.to-kousya.or.jp"
JKK_ENTRY = f"{JKK_BASE}/search/jkknet/service/akiyaJyoukenStartInit"

ALL_KU_VALUES     = ["21","11","09","07","01","05","18","22","13","12","16","03",
                     "19","17","04","06","14","10","23","08","15","02","20"]
ALL_SI_VALUES     = ["37","51","49","31","63","43","56-64","45","32","57","65","62",
                     "55","40","54","52","36","34","35","44","38","50","48","33","66",
                     "41","46-47","42","39","53"]
ALL_LAYOUT_VALUES = ["1","2","3","4"]

FWTABLE = str.maketrans(
    "１２３４５６７８９ＳＬＤＫＲＮＴＵＦＷＩＣｓｌｄｋ",
    "123456789SLDKRNTUFWICsldk"
)


class JKKScraper(BaseScraper):
    source = "jkk"

    # ── Public interface ──────────────────────────────────────────────────────

    def fetch_listings(self, config: dict) -> tuple[list[dict], bool]:
        """
        Scrape all current JKK vacancies.
        Also collects mz_copyright images while the session is alive.
        """
        all_listings = []
        ok = False
        try:
            with sync_playwright() as p:
                browser = self._launch_browser(p)
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
                    },
                )
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )

                splash = ctx.new_page()
                log.info("Opening JKK splash page…")

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
                self._wait_stable(popup)
                log.info(f"Condition search page: {popup.url}")

                with open("jkk_debug_form.html", "w", encoding="utf-8") as f:
                    f.write(popup.content())

                # Check all wards
                for value, name in [("ALLKU", "区部"), ("ALLSI", "市部")]:
                    cb = popup.locator(
                        f"input[name='akiyaInitRM.akiyaRefM.allCheck'][value='{value}']"
                    ).first
                    if cb.count() > 0 and not cb.is_checked():
                        log.info(f"Checking {name}…")
                        cb.check()
                        popup.wait_for_timeout(500)

                # Check all layouts
                for val in ALL_LAYOUT_VALUES:
                    cb = popup.locator(
                        f"input[name='akiyaInitRM.akiyaRefM.madoris'][value='{val}']"
                    ).first
                    if cb.count() > 0 and not cb.is_checked():
                        cb.check()

                # Submit
                log.info("Submitting search form…")
                search_btn = popup.locator("a[onclick*='akiyaJyoukenRef']").first
                search_btn.click()
                self._wait_stable(popup)
                log.info(f"Results page: {popup.url}")

                with open("jkk_debug_results.html", "w", encoding="utf-8") as f:
                    f.write(popup.content())

                if "空室はございませんでした" in popup.content():
                    log.info("No vacancies found.")
                    browser.close()
                    return [], True

                total_m = re.search(r"(\d+)件が該当", popup.content())
                total_expected = int(total_m.group(1)) if total_m else 0
                log.info(f"Total expected: {total_expected} listings")

                # Paginate
                page_num = 0
                while True:
                    page_num += 1
                    html = popup.content()

                    if "owabi" in html or "おわび" in html:
                        log.warning("Got error page — stopping")
                        break

                    new = self._parse_results(html, popup.url)
                    all_listings.extend(new)
                    log.info(f"Page {page_num}: {len(new)} listings (total: {len(all_listings)})")

                    if total_expected > 0 and len(all_listings) >= total_expected:
                        break
                    if page_num >= 50:
                        log.warning("Hit 50-page safety cap.")
                        break

                    next_btn = popup.locator("a[onclick*='afterPage']").first
                    if next_btn.count() == 0 or not next_btn.is_visible():
                        log.info("No more pages.")
                        break

                    next_btn.click()
                    self._wait_stable(popup)

                # Collect images while session is still alive
                try:
                    self._save_images_in_session(ctx, all_listings)
                except Exception as e:
                    log.warning(f"JKK in-session image collection failed: {e}")

                browser.close()
            ok = True

        except PWTimeout as e:
            log.error(f"JKK Playwright timeout: {e}")
        except Exception as e:
            log.error(f"JKK fetch failed: {e}")

        log.info(f"JKK: {len(all_listings)} listing(s) fetched")
        return all_listings, ok

    def fetch_images_batch(self, new_lids: list[str]) -> int:
        # JKK images are captured inside fetch_listings() while the session is alive.
        # Nothing to do here.
        return 0

    # ── Private helpers ───────────────────────────────────────────────────────

    def _launch_browser(self, p):
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
        try:
            return p.chromium.launch(headless=True, channel="chrome", args=args)
        except Exception:
            return p.chromium.launch(headless=True, args=args)

    def _wait_stable(self, page, timeout: int = 30_000):
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

    def _parse_results(self, html: str, current_url: str) -> list[dict]:
        if isinstance(html, bytes):
            html = html.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for row in soup.select("table tr"):
            tds = row.find_all("td")
            if len(tds) != 11 or row.find("th"):
                continue
            if not row.get_text(" ", strip=True):
                continue
            listing = self._extract_fields(tds, current_url)
            if listing:
                results.append(listing)
        return results

    def _extract_fields(self, tds, base_url: str) -> dict | None:
        # Column indices:
        # [0]=image [1]=name [2]=ward [3]=priority [4]=type [5]=layout
        # [6]=size [7]=rent [8]=kyoueki [9]=units [10]=detail
        if len(tds) < 10:
            return None

        name          = tds[1].get_text(strip=True)
        ward          = tds[2].get_text(strip=True)
        priority_text = tds[3].get_text(" ", strip=True) or None
        building_type = tds[4].get_text(strip=True) or None
        layout        = tds[5].get_text(strip=True).translate(FWTABLE)
        layout        = re.sub(r"S$", "", layout)
        if re.match(r"^[4-9]", layout):
            layout = "4LDK以上"

        size_text = tds[6].get_text(strip=True)
        rent_text = tds[7].get_text(strip=True)

        if name in ("住宅名", "") or ward in ("地域", ""):
            return None

        rent_raw = 0
        rent_m = re.search(r"(\d[\d,]+)", rent_text)
        if rent_m:
            rent_raw = int(rent_m.group(1).replace(",", ""))
        if rent_raw == 0:
            return None

        size_m2 = 0.0
        size_m = re.search(r"([\d.]+)", size_text)
        if size_m:
            try:
                size_m2 = float(size_m.group(1))
            except ValueError:
                pass

        url = base_url
        for td in tds:
            a = td.find("a", href=True)
            if a and a.get("href", "") not in ("", "#"):
                href = a["href"]
                url = (href if href.startswith("http")
                       else JKK_BASE + href if href.startswith("/")
                       else base_url)
                break

        thumbnail_url = None
        img = tds[0].find("img")
        if img and img.get("src"):
            src = img["src"]
            thumbnail_url = src if src.startswith("http") else (
                JKK_BASE + src if src.startswith("/") else None
            )

        return {
            "name": name, "address": ward, "ward": ward,
            "rent": rent_raw, "layout": layout, "size_m2": size_m2,
            "url": url, "source": "jkk", "thumbnail_url": thumbnail_url,
            "priority": priority_text, "building_type": building_type,
        }

    def _save_images_in_session(self, ctx, listings: list[dict]) -> None:
        """
        Download mz_copyright images while session cookies are still valid.
        Probes sequences 000–029 per listing; runs AI classification + OCR.
        """
        con = sqlite3.connect(self.db_file, timeout=60)
        con.execute("PRAGMA journal_mode=WAL")
        try:
            from scraper4 import listing_id  # avoid circular import at module level
            lids_done = {
                row[0] for row in
                con.execute("SELECT DISTINCT listing_id FROM listing_images").fetchall()
            }
            saved = 0
            for lst in listings:
                lid = listing_id(lst)
                if lid in lids_done:
                    continue
                thumb = lst.get("thumbnail_url") or ""
                m = re.search(r"mz_copyright/mobile/(\d+)/", thumb)
                if not m:
                    continue
                mz_id = m.group(1)

                img_dir = os.path.join(IMG_DIR, lid)
                os.makedirs(img_dir, exist_ok=True)

                imgs_saved = 0
                first_url  = None
                best_thumb: tuple | None = None  # (priority, url)

                for seq in range(30):
                    url = f"{JKK_BASE}/mz_copyright/mobile/{mz_id}/{mz_id}{seq:03d}.jpg"
                    try:
                        resp = ctx.request.get(url, timeout=10_000)
                        if resp.status != 200:
                            break
                        if not resp.headers.get("content-type", "").startswith("image"):
                            break
                        body = resp.body()

                        # Insert placeholder
                        con.execute(
                            "INSERT OR IGNORE INTO listing_images "
                            "(listing_id, url, image_type) VALUES (?,?,?)",
                            (lid, url, "interior"),
                        )
                        con.commit()
                        row_id = con.execute(
                            "SELECT id FROM listing_images WHERE listing_id=? AND url=?",
                            (lid, url),
                        ).fetchone()[0]

                        fpath = os.path.join(img_dir, f"{row_id}.jpg")
                        with open(fpath, "wb") as f:
                            f.write(body)

                        # AI classification
                        img_type = classify_image_ai(fpath)
                        if img_type == "skip":
                            os.remove(fpath)
                            con.execute("DELETE FROM listing_images WHERE id=?", (row_id,))
                            con.commit()
                            log.debug(f"JKK skipped junk seq {seq} ({mz_id})")
                            continue

                        ocr_text = None
                        if img_type == "floor_plan":
                            ocr_text = ocr_floor_plan(fpath)
                            if ocr_text:
                                log.info(f"JKK OCR {fpath}: {ocr_text[:80]}")
                            else:
                                img_type = "exterior"

                        con.execute(
                            "UPDATE listing_images "
                            "SET image_type=?, local_path=?, downloaded_at=?, ocr_text=? "
                            "WHERE id=?",
                            (img_type, fpath, datetime.now().isoformat(), ocr_text, row_id),
                        )
                        con.commit()

                        prio = THUMB_PRIORITY.get(img_type, 99)
                        if best_thumb is None or prio < best_thumb[0]:
                            best_thumb = (prio, url)
                        if first_url is None:
                            first_url = url
                        imgs_saved += 1

                    except Exception as e:
                        log.debug(f"JKK img seq {seq} failed ({mz_id}): {e}")
                        break

                if imgs_saved:
                    if best_thumb:
                        con.execute(
                            "UPDATE listings SET thumbnail_url=? WHERE id=?",
                            (best_thumb[1], lid),
                        )
                    con.commit()
                    saved += 1
                    log.info(f"JKK images: {imgs_saved} saved for {lst['name']}")
                    run_floor_plan_analysis(lid, con)

                lids_done.add(lid)
        finally:
            con.close()
        if saved:
            log.info(f"JKK session images: {saved} listing(s) processed.")
