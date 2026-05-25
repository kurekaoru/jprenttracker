"""
Base interface for all site-specific scrapers.

To add a new scraper:
  1. Create scraper_<site>.py and subclass BaseScraper.
  2. Set source = "<site>" and implement fetch_listings().
  3. Optionally override fetch_images_batch() and backfill_images().
  4. Add an instance to SCRAPERS in scraper4.py.
"""

import abc


class BaseScraper(abc.ABC):
    """
    Each site scraper must:
      - Set `source` to the DB source string ("jkk", "ur", …).
      - Implement `fetch_listings(config)`.
      - Override `fetch_images_batch` if images can only be scraped after
        the listing session (UR-style). Leave at default if images are
        captured during fetch_listings itself (JKK-style).
    """

    source: str  # overridden by subclass

    def __init__(self, db_file: str):
        self.db_file = db_file

    @abc.abstractmethod
    def fetch_listings(self, config: dict) -> tuple[list[dict], bool]:
        """
        Scrape current vacancies.

        Returns (listing_dicts, success_flag). Each listing dict must contain:
            name, address, ward, rent, layout, size_m2, url, source
        May also collect images when the live session is required (JKK).
        """

    def fetch_images_batch(self, new_lids: list[str]) -> int:
        """
        Fetch images for a batch of newly-seen listings.

        Called in a background thread; must open its own DB connection.
        Returns number of listings for which at least one image was saved.
        Default: 0 (scrapers that capture images during fetch_listings skip this).
        """
        return 0

    def backfill_images(self, listing_ids: list[str] | None = None, limit: int = 50) -> None:
        """Re-fetch images for listings with missing/broken image data."""
        pass
