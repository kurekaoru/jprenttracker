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

                # Open DB for image + detail collection during pagination
                from scraper4 import listing_id as lid_fn
                con = sqlite3.connect(self.db_file, timeout=60)
                con.execute("PRAGMA journal_mode=WAL")
                lids_done = {
                    row[0] for row in
                    con.execute("SELECT DISTINCT listing_id FROM listing_images").fetchall()
                }

                # Paginate — process detail pages inline
                page_num = 0
                try:
                    while True:
                        page_num += 1
                        html = popup.content()

                        if "owabi" in html or "おわび" in html:
                            log.warning("Got error page — stopping")
                            break

                        new = self._parse_results(html, popup.url)
                        all_listings.extend(new)
                        log.info(f"Page {page_num}: {len(new)} listings (total: {len(all_listings)})")

                        # Collect images + detail text for new listings on this page
                        for lst in new:
                            lid = lid_fn(lst)
                            if lid not in lids_done:
                                try:
                                    self._process_listing_detail(popup, ctx, lid, lst, con)
                                    lids_done.add(lid)
                                except Exception as e:
                                    log.warning(f"Detail scrape failed for {lst.get('name')}: {e}")

                        if total_expected > 0 and len(all_listings) >= total_expected:
                            break
                        if page_num >= 50:
                            log.warning("Hit 50-page safety cap.")
                            break

                        next_btn = popup.locator("a[onclick*='afterPage']").first
                        if next_btn.count() == 0 or not next_btn.is_visible():
                            log.info("No more pages.")
                            break

                        next_btn.click(timeout=60_000)
                        self._wait_stable(popup)
                finally:
                    con.close()

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

    def _process_listing_detail(self, popup, ctx, lid: str, lst: dict,
                                con: sqlite3.Connection) -> None:
        """
        Click a listing row on the current results page → navigate to its detail page
        → extract images + detail text → navigate back.
        """
        thumb = lst.get("thumbnail_url") or ""
        m = re.search(r"mz_copyright/mobile/(\d+)/", thumb)
        if not m:
            return
        mz_id = m.group(1)

        row_el = popup.locator(f"[onclick*='{mz_id}']").first
        if row_el.count() == 0:
            log.debug(f"No clickable row found for mz_id={mz_id}")
            return

        log.info(f"  → detail: {lst.get('name')} (mz_id={mz_id})")
        row_el.click(timeout=60_000)
        self._wait_stable(popup)

        try:
            popup.wait_for_selector(".sp-slide", timeout=15_000)
        except Exception:
            pass

        self._save_detail_images(popup, ctx, lid, con)

        back_btn = popup.locator("a[onclick*='akiyaBackRef']").first
        try:
            if back_btn.count() > 0:
                back_btn.click(timeout=60_000)
            else:
                popup.go_back()
            self._wait_stable(popup)
        except Exception as e:
            log.warning(f"  Back navigation failed: {e} — trying go_back()")
            try:
                popup.go_back()
                self._wait_stable(popup)
            except Exception:
                pass

    def _save_detail_images(self, page, ctx, lid: str, con: sqlite3.Connection) -> int:
        """Extract photos (slider DOM) + floor plan (openMzFile) + detail text from detail page."""
        from image_pipeline import extract_appliances, _merge_appliances

        img_dir = os.path.join(IMG_DIR, lid)
        os.makedirs(img_dir, exist_ok=True)

        # Photos from the slider
        photo_urls: list[str] = page.evaluate("""
            () => [...new Set(
                [...document.querySelectorAll('img.sp-image')]
                    .map(img => img.getAttribute('data-default') || img.src)
                    .filter(src => src && src.startsWith('http'))
            )]
        """)

        # Floor plan link
        fp_url: str | None = page.evaluate("""
            () => {
                const a = document.querySelector('a[onclick*="openMzFile"]');
                return a ? a.href : null;
            }
        """)

        # Detail text from info table cells
        detail_text: str = page.evaluate("""
            () => {
                const cells = [...document.querySelectorAll('td.Data_cell, td.Title_cell_conf')];
                if (cells.length > 0) return cells.map(c => c.innerText.trim()).filter(Boolean).join('\\n');
                const el = document.querySelector('td[width="685"]') || document.body;
                return el ? el.innerText : '';
            }
        """)
        if detail_text and detail_text.strip():
            lines = [l.strip() for l in detail_text.split("\n") if l.strip()]
            con.execute("UPDATE listings SET detail_text=? WHERE id=?",
                        ("\n".join(lines), lid))
            con.commit()

        all_urls = [(u, "interior") for u in photo_urls]
        if fp_url:
            all_urls.append((fp_url, "floor_plan"))

        saved = 0
        best_thumb: tuple | None = None

        for url, hint_type in all_urls:
            if not url or not url.startswith("http"):
                continue
            try:
                resp = ctx.request.get(url, timeout=10_000)
                if resp.status != 200:
                    continue
                ct = resp.headers.get("content-type", "")
                if not ct.startswith("image"):
                    continue
                body = resp.body()
                if len(body) < 2048:
                    continue

                con.execute(
                    "INSERT OR IGNORE INTO listing_images (listing_id, url, image_type) VALUES (?,?,?)",
                    (lid, url, hint_type),
                )
                con.commit()
                row_id = con.execute(
                    "SELECT id FROM listing_images WHERE listing_id=? AND url=?", (lid, url)
                ).fetchone()[0]

                fpath = os.path.join(img_dir, f"{row_id}.jpg")
                with open(fpath, "wb") as f:
                    f.write(body)

                img_type = classify_image_ai(fpath)
                if img_type == "skip":
                    os.remove(fpath)
                    con.execute("DELETE FROM listing_images WHERE id=?", (row_id,))
                    con.commit()
                    continue

                ocr_text = None
                if img_type == "floor_plan":
                    ocr_text = ocr_floor_plan(fpath)
                    if ocr_text:
                        log.info(f"    OCR {lid[:8]}: {ocr_text[:80]}")
                    else:
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
                    best_thumb = (prio, url)
                saved += 1

            except Exception as e:
                log.warning(f"  Image download failed {url}: {e}")

        if saved:
            if best_thumb:
                url_to_id = dict(con.execute(
                    "SELECT url, id FROM listing_images WHERE listing_id=?", (lid,)
                ).fetchall())
                img_id = url_to_id.get(best_thumb[1])
                thumb = f"/api/images/{img_id}" if img_id else best_thumb[1]
                con.execute("UPDATE listings SET thumbnail_url=? WHERE id=?", (thumb, lid))
            con.commit()
            log.info(f"    {saved} image(s) saved for {lid[:8]}")
            run_floor_plan_analysis(lid, con)

        return saved
