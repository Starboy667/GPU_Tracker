"""Rebuild tests/fixtures from live pages: one request per page, trimmed to a few product nodes.

Run:  .venv/bin/python tools/make_fixtures.py [--raw-dir DIR] [site ...]

With --raw-dir, full pages are cached there (``<name>.html``) and reused on the next run, so the
parsers can be reworked without hitting the sites again.

tests/test_scrapers_sites.py asserts values taken from the committed fixtures: after regenerating,
update the expected values there.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pricewatch.scrapers import alternate, dealabs, grosbill, idealo, ldlc, materiel, topachat  # noqa: E402
from pricewatch.scrapers.base import FetchContext  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
QUERY = "rtx 5070"
TOPACHAT_CATEGORY = (
    "https://www.topachat.com/pages/produits_cat_est_micro_puis_rubrique_est_wgfx_pcie_puis_f_est_58-13420.html"
)
HTML_SITES = {m.KEY: m for m in (ldlc, materiel, grosbill, alternate, idealo)}
NODES_PER_FIXTURE = 4
NOISE_TAGS = ("script", "style", "svg", "noscript")


class Pages:
    """Fetches pages, through an optional on-disk cache of the full responses."""

    def __init__(self, raw_dir: Path | None):
        self.ctx = FetchContext()
        self.raw_dir = raw_dir

    def get(self, name: str, url: str, *, browser: bool = False) -> str:
        cached = self.raw_dir / f"{name}.html" if self.raw_dir else None
        if cached and cached.exists():
            return cached.read_text(encoding="utf-8")
        text = self.ctx.get_as_browser(url) if browser else self.ctx.get(url)
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(text, encoding="utf-8")
        time.sleep(1.5)
        return text


def header(url: str) -> str:
    return f"<!-- {url} — fetched {date.today().isoformat()}, trimmed by tools/make_fixtures.py -->\n"


def pick_varied(nodes: list, stock_of) -> list:
    """Up to NODES_PER_FIXTURE nodes, in page order, covering every stock state seen."""
    chosen: dict[int, object] = {}
    seen_states = set()
    for i, node in enumerate(nodes):
        state = stock_of(node)
        if state not in seen_states:
            seen_states.add(state)
            chosen[i] = node
    for i, node in enumerate(nodes):
        if len(chosen) >= NODES_PER_FIXTURE:
            break
        chosen.setdefault(i, node)
    return [chosen[i] for i in sorted(chosen)][:NODES_PER_FIXTURE]


def html_fixture(module, pages: Pages) -> str:
    url = module.search_url(QUERY)
    soup = BeautifulSoup(pages.get(module.KEY, url, browser=module is idealo), "lxml")
    nodes = [n for n in soup.select(module.ITEM_SELECTOR) if module.parse(str(n))]
    if not nodes:
        raise SystemExit(f"{module.KEY}: no product parsed from {url}; the selectors are probably stale")

    def stock_of(node):
        return module.parse(str(node))[0].in_stock

    picked = pick_varied(nodes, stock_of)
    for node in picked:
        for noise in node.find_all(NOISE_TAGS):
            noise.decompose()
    body = "\n".join(str(n) for n in picked)
    return f"{header(url)}<html><body><div>\n{body}\n</div></body></html>\n"


def topachat_products(html: str) -> list[str]:
    """The source text of each product object, cut at the start of the next one."""
    starts = [m.start() for m in re.finditer(r'\{ref:"in\d+",label:"', html)]
    return [html[a:b].rstrip(",") for a, b in zip(starts, starts[1:])]


def topachat_fixture(pages: Pages) -> str:
    category = topachat_products(pages.get("topachat_category", TOPACHAT_CATEGORY))[:3]
    search_url = topachat.search_url(QUERY)
    accented = [p for p in topachat_products(pages.get("topachat", search_url)) if re.search(r"[^\x00-\x7f]", p[:120])]
    products = category + accented[:1]
    if not products:
        raise SystemExit("topachat: no product object found; the page format has probably changed")
    payload = ",".join(products)
    return (
        f"{header(TOPACHAT_CATEGORY)}{header(search_url)}"
        f"<html><body><script>window.__NUXT__={{data:[{{products:[{payload}]}}]}}</script></body></html>\n"
    )


def dealabs_fixture(pages: Pages) -> str:
    xml = pages.get("dealabs", dealabs.GPU_FEED)
    head = xml[: xml.index("<item>")]
    items = re.findall(r"<item>.*?</item>", xml, re.S)[:NODES_PER_FIXTURE]
    # The description is community-written HTML the parser never reads: keep the tag, drop the text.
    items = [re.sub(r"<description>.*?</description>", "<description>…</description>", i, flags=re.S) for i in items]
    return head + "\n".join(items) + "\n</channel></rss>\n"


def main() -> None:
    known = [*HTML_SITES, topachat.KEY, dealabs.KEY]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sites", nargs="*", metavar="site", help=f"one of {', '.join(known)} (default: all)")
    ap.add_argument("--raw-dir", type=Path, help="cache full pages here")
    args = ap.parse_args()
    unknown = [s for s in args.sites if s not in known]
    if unknown:
        ap.error(f"unknown site(s): {', '.join(unknown)}")
    pages = Pages(args.raw_dir)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for site in args.sites or known:
        if site in HTML_SITES:
            name, text = f"{site}.html", html_fixture(HTML_SITES[site], pages)
        elif site == topachat.KEY:
            name, text = "topachat.html", topachat_fixture(pages)
        else:
            name, text = "dealabs.xml", dealabs_fixture(pages)
        (FIXTURES / name).write_text(text, encoding="utf-8")
        print(f"{name:<16} {len(text.encode('utf-8')):>6} bytes")


if __name__ == "__main__":
    main()
