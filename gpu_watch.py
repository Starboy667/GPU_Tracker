#!/usr/bin/env python3
"""GPU price watcher for French retailers, with Discord alerts.

Usage:
    python gpu_watch.py                 # one check, alert on Discord if a deal is found
    python gpu_watch.py --dry-run       # print what would be sent, don't touch Discord/state
    python gpu_watch.py --list          # show every matching listing, cheapest first
    python gpu_watch.py --loop 30       # check every 30 minutes forever
    python gpu_watch.py --test-discord  # send a test message to the webhook
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
HISTORY_FILE = ROOT / "price_history.csv"
STATS_FILE = ROOT / "price_stats.json"  # {watch: {"YYYY-MM-DD": lowest available price that day}}
STATS_KEEP_DAYS = 120

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class Listing:
    site: str
    title: str
    price: float
    url: str
    in_stock: bool | None  # None = unknown


# --------------------------------------------------------------------------- helpers

def parse_price(text: str) -> float | None:
    """Parse French-style prices: '1 079€95', '€ 1.029,00', '1 249,99 €', '549.99'."""
    if not text:
        return None
    t = text.replace("\xa0", "").replace(" ", "").replace(" ", "")
    m = re.search(r"(\d[\d.]*)€(\d{2})\b", t)  # LDLC / Materiel.net: 1079€95
    if m:
        return float(m.group(1).replace(".", "") + "." + m.group(2))
    t = t.replace("€", "")
    m = re.search(r"\d[\d.,]*", t)
    if not m:
        return None
    num = m.group(0)
    if "," in num:  # 1.029,00 -> 1029.00
        num = num.replace(".", "").replace(",", ".")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


def stock_from_text(text: str) -> bool | None:
    t = text.lower()
    if any(w in t for w in ("rupture", "épuisé", "indisponible", "non dispo", "sur commande", "précommande")):
        return False
    if any(w in t for w in ("en stock", "stock limité", "disponible", "expédié")) or re.search(r"sous \d+ jours", t):
        return True
    return None


def fetch(session: requests.Session, url: str) -> str:
    for attempt in range(3):
        try:
            r = session.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.text
            err = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            err = str(e)
        time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{url}: {err}")


def fetch_as_browser(url: str) -> str:
    """For sites that reject plain HTTP clients: impersonate a real Chrome TLS/HTTP2 fingerprint."""
    try:
        from curl_cffi import requests as cr
    except ImportError:
        raise RuntimeError("needs curl_cffi: .venv/bin/pip install curl_cffi")
    err = ""
    for attempt in range(2):
        try:
            r = cr.get(url, impersonate="chrome", timeout=25,
                       headers={"Accept-Language": HEADERS["Accept-Language"]})
            if r.status_code == 200:
                return r.text
            err = f"HTTP {r.status_code} (blocked?)"
        except Exception as e:
            err = str(e)
        time.sleep(3 + attempt * 5)
    raise RuntimeError(f"{url}: {err}")


def text_of(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


# --------------------------------------------------------------------------- scrapers
# Each scraper: (session, query, url_override) -> list[Listing]

def scrape_ldlc(session, query, url=None):
    url = url or f"https://www.ldlc.com/recherche/{quote_plus(query).replace('+', '%20')}/"
    soup = BeautifulSoup(fetch(session, url), "lxml")
    out = []
    for li in soup.select("li.pdt-item"):
        a = li.select_one("h3 a")
        price = parse_price(li.select_one(".price").get_text("", strip=True) if li.select_one(".price") else "")
        if not a or price is None:
            continue
        out.append(Listing("LDLC", text_of(a), price, urljoin("https://www.ldlc.com", a["href"]),
                           stock_from_text(text_of(li.select_one(".stock")))))
    return out


def scrape_materiel(session, query, url=None):
    url = url or f"https://www.materiel.net/recherche/{quote_plus(query).replace('+', '%20')}/"
    soup = BeautifulSoup(fetch(session, url), "lxml")
    out = []
    for li in soup.select("li.c-products-list__item"):
        a = li.select_one("a.c-product__link")
        p = li.select_one(".o-product__price")
        price = parse_price(p.get_text("", strip=True)) if p else None
        if not a or price is None:
            continue
        title = a.get("title") or text_of(li.select_one(".c-product__title"))
        stock = stock_from_text(text_of(li.select_one(".o-availability__value")))
        out.append(Listing("Materiel.net", title, price, urljoin("https://www.materiel.net", a["href"]), stock))
    return out


def scrape_topachat(session, query, url=None):
    # TopAchat is a Nuxt app: product data lives in an inline JS object, not in the HTML.
    url = url or f"https://www.topachat.com/pages/recherche.php?q={quote_plus(query)}"
    html = fetch(session, url)
    out = []
    for m in re.finditer(r'\{ref:"(in\d+)",label:"((?:[^"\\]|\\.)*)"', html):
        seg = html[m.start(): m.start() + 2500]
        pf = re.search(r"price_final:([\d.]+)", seg)
        link = re.search(r'url_product:"([^"]+)"', seg)
        avail = re.search(r'availability:\{code:"([^"]+)"', seg)
        if not pf:
            continue
        stock = None if not avail else avail.group(1) in ("available", "limited-stock")
        href = urljoin("https://www.topachat.com", link.group(1).encode().decode("unicode_escape")) if link else url
        title = m.group(2).encode().decode("unicode_escape", errors="ignore")
        out.append(Listing("TopAchat", title, float(pf.group(1)), href, stock))
    return out


def scrape_grosbill(session, query, url=None):
    url = url or f"https://www.grosbill.com/catv2.cgi?mode=recherche&recherche={quote_plus(query)}"
    soup = BeautifulSoup(fetch(session, url), "lxml")
    out = []
    for art in soup.select("article.product-row"):
        a = art.select_one("a.p-name")
        price = parse_price(art.get("data-price", "")) or parse_price(text_of(art.select_one(".price-main")))
        if not a or price is None:
            continue
        dispo = art.select_one(".dw-status")
        stock = None
        if dispo:
            stock = "dw-out" not in (dispo.get("class") or [])
        out.append(Listing("Grosbill", text_of(a), price, urljoin("https://www.grosbill.com", a["href"]), stock))
    return out


def scrape_alternate(session, query, url=None):
    url = url or f"https://www.alternate.fr/listing.xhtml?q={quote_plus(query)}"
    soup = BeautifulSoup(fetch(session, url), "lxml")
    out = []
    for a in soup.select("a.productBox"):
        price = parse_price(text_of(a.select_one("span.price")))
        title = text_of(a.select_one(".product-name"))
        if not title or price is None:
            continue
        stock = stock_from_text(text_of(a.select_one(".delivery-info")))
        out.append(Listing("Alternate", title, price, urljoin("https://www.alternate.fr", a["href"]), stock))
    return out


def parse_idealo(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for item in soup.select("[data-testid=result-item]"):
        a = item.select_one("[data-testid=resultItemLink] a[href]")
        title = text_of(item.select_one("[data-testid=product-summary-title]"))
        price = parse_price(text_of(item.select_one("[data-testid=detailed-price-info-price]")))
        if not a or not title or price is None:
            continue
        offers = re.search(r"(\d+)\s*offres?", text_of(item.select_one("[data-testid=detailed-price-info]")))
        site = f"Idealo ({offers.group(1)} offres)" if offers else "Idealo"
        # Idealo only lists products with live offers; the price is the cheapest shop's.
        out.append(Listing(site, title, price, urljoin("https://www.idealo.fr", a["href"]), None))
    return out


def scrape_idealo(session, query, url=None):
    # Price comparator: one listing per product, at its lowest price across all shops.
    # Blocks plain HTTP clients, hence fetch_as_browser.
    url = url or f"https://www.idealo.fr/prechcat.html?q={quote_plus(query)}"
    return parse_idealo(fetch_as_browser(url))


_feed_cache: dict[str, str] = {}
DEALABS_MAX_AGE_DAYS = 3  # overridden by defaults.dealabs_max_age_days


def scrape_dealabs(session, query, url=None):
    # Community deals via the public RSS feed of the "cartes graphiques" group. One feed serves
    # every watch (filtered by `match`), so it's fetched once per run. Covers merchants we can't
    # scrape directly (Amazon, Cdiscount, Darty, ...). Stock is unknown; deals may have expired.
    url = url or "https://www.dealabs.com/rss/groupe/cartes-graphiques"
    if url not in _feed_cache:
        _feed_cache[url] = fetch(session, url)
    soup = BeautifulSoup(_feed_cache[url], "xml")
    out = []
    for item in soup.find_all("item"):
        merchant = item.find("merchant")
        price = parse_price(merchant.get("price", "")) if merchant else None
        if price is None or not item.title or not item.link:
            continue
        if item.pubDate:
            try:
                posted = parsedate_to_datetime(item.pubDate.get_text(strip=True))
                if datetime.now(timezone.utc) - posted > timedelta(days=DEALABS_MAX_AGE_DAYS):
                    continue
            except (TypeError, ValueError):
                pass
        shop = merchant.get("name")
        out.append(Listing(f"Dealabs/{shop}" if shop else "Dealabs", item.title.get_text(strip=True), price,
                           item.link.get_text(strip=True), None))
    return out


SCRAPERS = {
    "ldlc": scrape_ldlc,
    "materiel": scrape_materiel,
    "topachat": scrape_topachat,
    "grosbill": scrape_grosbill,
    "alternate": scrape_alternate,
    "dealabs": scrape_dealabs,
    "idealo": scrape_idealo,
}


# --------------------------------------------------------------------------- core

def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def append_history(rows: list[tuple[str, Listing]]):
    new = not HISTORY_FILE.exists()
    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "watch", "site", "title", "price", "in_stock", "url"])
        ts = datetime.now().isoformat(timespec="seconds")
        for watch, l in rows:
            w.writerow([ts, watch, l.site, l.title, f"{l.price:.2f}", l.in_stock, l.url])


# --------------------------------------------------------------------------- price baseline
#
# For each watch we store one number per day: the lowest price at which the card was available
# (in stock or unknown) at a regular retailer. Dealabs is left out: its posts are already deals and
# would drag the "normal" price down. The baseline is the median of those daily lows over the last
# `baseline_days` days, *excluding today* so an ongoing deal can't lower its own reference.

def counts_for_baseline(l: Listing) -> bool:
    return l.in_stock is not False and not l.site.lower().startswith("dealabs")


def load_stats() -> dict:
    if STATS_FILE.exists():
        return json.loads(STATS_FILE.read_text())
    return seed_stats_from_history()


def seed_stats_from_history() -> dict:
    """First run with the stats file missing: rebuild daily lows from price_history.csv."""
    stats: dict = {}
    if not HISTORY_FILE.exists():
        return stats
    with HISTORY_FILE.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                l = Listing(row["site"], row["title"], float(row["price"]), row["url"],
                            {"True": True, "False": False}.get(row["in_stock"]))
                day = row["timestamp"][:10]
            except (KeyError, ValueError):
                continue
            if counts_for_baseline(l):
                days = stats.setdefault(row["watch"], {})
                days[day] = min(days.get(day, l.price), l.price)
    return stats


def record_stats(stats: dict, results: dict[str, list[Listing]]):
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=STATS_KEEP_DAYS)).isoformat()
    for name, listings in results.items():
        prices = [l.price for l in listings if counts_for_baseline(l)]
        days = stats.setdefault(name, {})
        if prices:
            days[today] = min(days.get(today, min(prices)), min(prices))
        for d in [d for d in days if d < cutoff]:
            del days[d]
    STATS_FILE.write_text(json.dumps(stats, indent=1, sort_keys=True))


def baseline(stats: dict, name: str, defaults: dict) -> tuple[float, int] | None:
    """(median daily low, number of days used), or None if there isn't enough history yet."""
    span = defaults.get("baseline_days", 14)
    today = date.today()
    start = (today - timedelta(days=span)).isoformat()
    lows = [p for d, p in stats.get(name, {}).items() if start <= d < today.isoformat()]
    if len(lows) < defaults.get("baseline_min_days", 3):
        return None
    return statistics.median(lows), len(lows)


def deal_threshold(watch: dict, defaults: dict, base: tuple[float, int] | None) -> float | None:
    """Alert at or below this price: the larger of max_price and baseline − deal_drop_pct %."""
    limits = []
    if watch.get("max_price") is not None:
        limits.append(watch["max_price"])
    if base:
        drop = watch.get("deal_drop_pct", defaults.get("deal_drop_pct", 8))
        limits.append(base[0] * (1 - drop / 100))
    return max(limits) if limits else None


VARIANT_SUFFIXES = ("ti", "xt", "xtx", "super", "gre")


def match_from_name(name: str) -> str:
    """'RX 9070 XT' -> 'RX\\s*9070\\s*XT'; 'RTX 5070' also refuses Ti/Super/XT variants."""
    tokens = name.split()
    pattern = r"\s*".join(re.escape(t) for t in tokens)
    if tokens and tokens[-1].lower() not in VARIANT_SUFFIXES:
        pattern += r"(?![\s-]*(?:" + "|".join(VARIANT_SUFFIXES) + r")\b)"
    return pattern


def validate_config(config: dict):
    errors = []
    watches = (config or {}).get("watches")
    if not isinstance(watches, list) or not watches:
        errors.append("`watches:` must be a non-empty list")
        watches = []
    for i, w in enumerate(watches, 1):
        if not isinstance(w, dict) or not w.get("name"):
            errors.append(f"watch #{i}: missing `name`")
            continue
        if w.get("max_price") is not None and not isinstance(w["max_price"], (int, float)):
            errors.append(f"watch '{w['name']}': `max_price` must be a number (in €) or left out")
        w.setdefault("match", match_from_name(str(w["name"])))
        for key in ("match", "exclude"):
            if w.get(key):
                try:
                    re.compile(w[key])
                except re.error as e:
                    errors.append(f"watch '{w['name']}': bad `{key}` regex: {e}")
    if errors:
        sys.exit("config.yaml problems:\n  - " + "\n  - ".join(errors))


def matches(listing: Listing, watch: dict, defaults: dict) -> bool:
    title = listing.title
    if not re.search(watch["match"], title, re.I):
        return False
    excl = [defaults.get("exclude"), watch.get("exclude")]
    if any(e and re.search(e, title, re.I) for e in excl):
        return False
    min_p = watch.get("min_price", defaults.get("min_price", 0))
    return listing.price >= min_p


def collect(config: dict, verbose=True) -> dict[str, list[Listing]]:
    global DEALABS_MAX_AGE_DAYS
    defaults = config.get("defaults", {})
    DEALABS_MAX_AGE_DAYS = defaults.get("dealabs_max_age_days", DEALABS_MAX_AGE_DAYS)
    enabled = config.get("sites", list(SCRAPERS))
    session = requests.Session()
    results: dict[str, list[Listing]] = {}
    for watch in config["watches"]:
        name = watch["name"]
        found: dict[str, Listing] = {}
        for site in watch.get("sites", enabled):
            scraper = SCRAPERS.get(site)
            if not scraper:
                print(f"  ! unknown site '{site}'", file=sys.stderr)
                continue
            url = (watch.get("urls") or {}).get(site)
            try:
                listings = scraper(session, watch.get("query", name), url)
            except Exception as e:
                print(f"  ! {site} failed for {name}: {e}", file=sys.stderr)
                continue
            kept = [l for l in listings if matches(l, watch, defaults)]
            if verbose:
                print(f"  {name:<14} {site:<10} {len(listings):>3} listings, {len(kept):>3} match")
            for l in kept:
                found.setdefault(l.url, l)
            time.sleep(random.uniform(1.5, 3.5))  # be polite
        results[name] = sorted(found.values(), key=lambda l: l.price)
    return results


def fmt_eur(p: float) -> str:
    return f"{p:,.2f} €".replace(",", " ").replace(".", ",")


def send_discord(webhook: str, content: str | None = None, embeds: list | None = None):
    for i in range(0, max(len(embeds or []), 1), 10):  # Discord: max 10 embeds per message
        payload = {"username": "GPU Watch"}
        if content and i == 0:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds[i:i + 10]
        r = requests.post(webhook, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            r = requests.post(webhook, json=payload, timeout=15)
        r.raise_for_status()


def make_embed(watch: dict, l: Listing, previous: float | None, base: tuple[float, int] | None) -> dict:
    desc = f"**{fmt_eur(l.price)}**"
    if base:
        pct = (l.price / base[0] - 1) * 100
        desc += f" ({pct:+.0f} % vs prix habituel {fmt_eur(base[0])}, médiane sur {base[1]} j)"
    if watch.get("max_price") is not None:
        desc += f"\nCible : {fmt_eur(watch['max_price'])}"
    if previous:
        desc += f"\nAvant : ~~{fmt_eur(previous)}~~"
    stock = {True: "✅ En stock", False: "❌ Rupture", None: "❔ Inconnu"}[l.in_stock]
    return {
        "title": f"{watch['name']} — {l.site}"[:256],
        "url": l.url,
        "description": f"{l.title}\n{desc}"[:4000],
        "color": 0x2ECC71 if l.in_stock else 0xF1C40F,
        "fields": [{"name": "Stock", "value": stock, "inline": True}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def check(config: dict, dry_run=False):
    defaults = config.get("defaults", {})
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] checking prices…")
    results = collect(config)
    state = load_state()
    stats = load_stats()
    renotify_drop = defaults.get("renotify_drop", 10)
    embeds, history = [], []

    for watch in config["watches"]:
        name = watch["name"]
        listings = results.get(name, [])
        history += [(name, l) for l in listings]
        in_stock_only = watch.get("in_stock_only", defaults.get("in_stock_only", True))
        base = baseline(stats, name, defaults)
        limit = deal_threshold(watch, defaults, base)
        if listings:
            best = listings[0]
            ref = f", usual {fmt_eur(base[0])}" if base else ", no baseline yet"
            print(f"  → {name}: cheapest {fmt_eur(best.price)} at {best.site} ({best.title[:50]}){ref}")

        for l in listings:
            key = l.url
            prev = state.get(key, {}).get("notified_price")
            is_deal = (limit is not None and l.price <= limit
                       and (l.in_stock is not False or not in_stock_only))
            if not is_deal:
                # Back above target (or out of stock): forget so we alert again next time it dips.
                if prev is not None and not dry_run:
                    state.pop(key, None)
                continue
            if prev is not None and l.price > prev - renotify_drop:
                continue  # already notified at this price (or close)
            embeds.append(make_embed(watch, l, prev, base))
            if not dry_run:
                state[key] = {"notified_price": l.price, "title": l.title, "at": datetime.now().isoformat()}

    if dry_run:
        print(f"\n[dry-run] {len(embeds)} alert(s) would be sent:")
        for e in embeds:
            print(f"  - {e['title']}: {e['description'].splitlines()[-1]}  {e['url']}")
        return

    append_history(history)
    record_stats(stats, results)
    if embeds:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL") or config.get("discord_webhook")
        if not webhook:
            print("  ! deals found but no DISCORD_WEBHOOK_URL set", file=sys.stderr)
        else:
            send_discord(webhook, content=f"🔥 {len(embeds)} bon(s) plan(s) GPU", embeds=embeds)
            print(f"  sent {len(embeds)} alert(s) to Discord")
    else:
        print("  no new deals")
    save_state(state)


def list_all(config: dict):
    defaults = config.get("defaults", {})
    results = collect(config)
    stats = load_stats()
    for watch in config["watches"]:
        base = baseline(stats, watch["name"], defaults)
        limit = deal_threshold(watch, defaults, base)
        info = [f"usual {fmt_eur(base[0])} over {base[1]} d" if base else "no baseline yet"]
        if limit is not None:
            info.append(f"deal ≤ {fmt_eur(limit)}")
        print(f"\n=== {watch['name']} ({', '.join(info)})")
        for l in results.get(watch["name"], []):
            flag = "🔥" if limit is not None and l.price <= limit else "  "
            stock = {True: "stock", False: "rupt.", None: "?"}[l.in_stock]
            print(f" {flag} {fmt_eur(l.price):>12}  {l.site:<18} {stock:<5}  {l.title[:70]}")
    record_stats(stats, results)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--loop", type=float, metavar="MINUTES")
    ap.add_argument("--test-discord", action="store_true")
    args = ap.parse_args()

    load_env()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    validate_config(config)

    if args.test_discord:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL") or config.get("discord_webhook")
        if not webhook:
            sys.exit("Set DISCORD_WEBHOOK_URL in .env first")
        send_discord(webhook, content="✅ GPU Watch est bien connecté.")
        print("sent")
        return
    if args.list:
        list_all(config)
        return
    if args.loop:
        while True:
            try:
                check(config, args.dry_run)
            except Exception as e:  # keep the loop alive
                print(f"  ! check failed: {e}", file=sys.stderr)
            time.sleep(args.loop * 60 * random.uniform(0.9, 1.1))
    check(config, args.dry_run)


if __name__ == "__main__":
    main()
