"""Scraper registry.

``SCRAPERS`` maps a site key to its ``SiteScraper`` (search/category pages of a known site).
``scrape_product_page`` follows a single product URL on any shop.
"""
from __future__ import annotations

from pricewatch import netsafe
from pricewatch.scrapers import alternate, dealabs, grosbill, idealo, ldlc, materiel, topachat
from pricewatch.scrapers.base import SiteScraper
from pricewatch.scrapers.product_page import parse_product_page, scrape_product_page, site_label

SCRAPERS: dict[str, SiteScraper] = {
    module.SCRAPER.key: module.SCRAPER
    for module in (ldlc, materiel, topachat, grosbill, alternate, idealo, dealabs)
}
DEFAULT_SITES: tuple[str, ...] = tuple(SCRAPERS)
DEALABS_GPU_FEED = dealabs.GPU_FEED


def validate_site_url(site_key: str, url: str) -> str:
    """Check a URL override for a known site: it must be an http(s) URL on that site's domain.

    Returns the stripped URL; raises ``netsafe.UnsafeURLError`` with a message for the user.
    """
    scraper = SCRAPERS.get(site_key)
    if scraper is None:
        raise netsafe.UnsafeURLError(f"unknown site '{site_key}'")
    return netsafe.check_url(url, allowed_domains=scraper.domains)


__all__ = [
    "DEALABS_GPU_FEED",
    "DEFAULT_SITES",
    "SCRAPERS",
    "parse_product_page",
    "scrape_product_page",
    "site_label",
    "validate_site_url",
]
