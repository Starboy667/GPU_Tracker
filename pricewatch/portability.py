"""Import and export watches as YAML.

Reads both this app's own export (``version: 2``) and the ``config.yaml`` of the original
gpu_watch.py script, so an existing list of cards can be brought over in one paste.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable

import yaml

from pricewatch import deals, netsafe
from pricewatch.forms import (
    MAX_DROP_PCT,
    MAX_NAME_LENGTH,
    MAX_PRICE,
    MAX_PRODUCT_URLS,
    MAX_QUERY_LENGTH,
    MAX_SELECTOR_LENGTH,
    MIN_DROP_PCT,
)
from pricewatch.models import VISIBILITY_PRIVATE, VISIBILITY_PUBLIC, ProductURL, Watch
from pricewatch.presets import DEALABS_GPU_FEED, ORIGINAL_SITES

EXPORT_VERSION = 2
MAX_IMPORT_BYTES = 200_000
MAX_WATCHES = 200


class ImportError_(ValueError):
    """The document cannot be imported. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _number(value, label: str, problems: list[str], *, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        problems.append(f"{label} must be a number")
        return None
    if not low <= value <= high:
        problems.append(f"{label} must be between {low:g} and {high:g}")
        return None
    return float(value)


def _text(raw: dict, key: str, label: str, problems: list[str], *, limit: int) -> str:
    """A string field of a watch: '' when absent, a recorded problem when it is not text."""
    value = raw.get(key)
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)  # `query: 5070` is a legitimate thing to write
    if not isinstance(value, str):
        problems.append(f"{label}: `{key}` must be text")
        return ""
    if len(value) > limit:
        problems.append(f"{label}: `{key}` is too long")
        return ""
    return value.strip()


def _combine_excludes(*patterns: str | None) -> str:
    kept = [p for p in patterns if p]
    if len(kept) <= 1:
        return kept[0] if kept else ""
    return "|".join(f"(?:{p})" for p in kept)


def import_yaml(
    text: str,
    *,
    site_keys: Iterable[str],
    validate_site_url: Callable[[str, str], str],
) -> list[Watch]:
    """Parse a YAML document into watches (not saved). Raises ``ImportError_`` listing all problems."""
    if len(text.encode()) > MAX_IMPORT_BYTES:
        raise ImportError_(["the document is too large"])
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise ImportError_([f"not valid YAML{where}"]) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("watches"), list) or not doc["watches"]:
        raise ImportError_(["expected a `watches:` list"])
    if len(doc["watches"]) > MAX_WATCHES:
        raise ImportError_([f"at most {MAX_WATCHES} watches per import"])

    known = tuple(site_keys)
    legacy = doc.get("version") != EXPORT_VERSION
    defaults = doc.get("defaults") if isinstance(doc.get("defaults"), dict) else {}
    # A document without `sites:` is the original script's: it only knew the hardware shops.
    default_sites = doc.get("sites") if isinstance(doc.get("sites"), list) else [s for s in ORIGINAL_SITES if s in known]
    problems: list[str] = []
    watches: list[Watch] = []
    seen: set[str] = set()

    for index, raw in enumerate(doc["watches"], start=1):
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            problems.append(f"watch #{index}: missing `name`")
            continue
        name = str(raw["name"]).strip()
        label = f"watch '{name}'"
        if len(name) > MAX_NAME_LENGTH:
            problems.append(f"{label}: name too long")
        if name.lower() in seen:
            problems.append(f"{label}: listed twice")
        seen.add(name.lower())

        match = _text(raw, "match", label, problems, limit=deals.MAX_REGEX_LENGTH) or deals.auto_match(name)
        default_exclude = defaults.get("exclude") if legacy and isinstance(defaults.get("exclude"), str) else None
        exclude = _combine_excludes(
            default_exclude, _text(raw, "exclude", label, problems, limit=deals.MAX_REGEX_LENGTH)
        )
        for key, pattern in (("match", match), ("exclude", exclude)):
            problem = deals.regex_problem(pattern) if pattern else None
            if problem:
                problems.append(f"{label}: `{key}` {problem}")

        sites_raw = raw.get("sites", default_sites)
        sites = tuple(str(s) for s in sites_raw) if isinstance(sites_raw, list) else ()
        for site in sites:
            if site not in known:
                problems.append(f"{label}: unknown site '{site}'")

        site_urls: dict[str, str] = {}
        urls_raw = raw.get("urls") if isinstance(raw.get("urls"), dict) else {}
        for site, url in urls_raw.items():
            try:
                site_urls[str(site)] = validate_site_url(str(site), str(url))
            except netsafe.UnsafeURLError as exc:
                problems.append(f"{label}: URL for {site}: {exc}")
        if legacy and "dealabs" in sites and "dealabs" not in site_urls:
            site_urls["dealabs"] = DEALABS_GPU_FEED  # the original script only read the GPU feed

        pages: list[ProductURL] = []
        pages_raw = raw.get("product_urls") if isinstance(raw.get("product_urls"), list) else []
        for entry in pages_raw[:MAX_PRODUCT_URLS + 1]:
            url, selector = (entry.get("url"), entry.get("selector", "")) if isinstance(entry, dict) else (entry, "")
            if not isinstance(selector, str) or len(selector) > MAX_SELECTOR_LENGTH:
                problems.append(f"{label}: product page: `selector` must be text of at most {MAX_SELECTOR_LENGTH} characters")
                continue
            try:
                pages.append(ProductURL(netsafe.check_url(str(url or "")), selector.strip()))
            except netsafe.UnsafeURLError as exc:
                problems.append(f"{label}: product page: {exc}")
        if len(pages_raw) > MAX_PRODUCT_URLS:
            problems.append(f"{label}: at most {MAX_PRODUCT_URLS} product pages")
        if not sites and not pages:
            problems.append(f"{label}: no site and no product page")

        in_stock_only = raw.get("in_stock_only")
        watches.append(Watch(
            name=name,
            query=_text(raw, "query", label, problems, limit=MAX_QUERY_LENGTH) or name,
            match=match,
            exclude=exclude,
            min_price=_number(raw.get("min_price", defaults.get("min_price") if legacy else None),
                              f"{label}: `min_price`", problems, low=0, high=MAX_PRICE),
            max_price=_number(raw.get("max_price"), f"{label}: `max_price`", problems, low=0, high=MAX_PRICE),
            deal_drop_pct=_number(raw.get("deal_drop_pct"), f"{label}: `deal_drop_pct`", problems,
                                  low=MIN_DROP_PCT, high=MAX_DROP_PCT),
            in_stock_only=in_stock_only if isinstance(in_stock_only, bool) else None,
            notify_restock=raw.get("notify_restock") is True,
            sites=sites,
            site_urls=site_urls,
            product_urls=tuple(pages),
            enabled=raw.get("enabled") is not False,
            # "shared" names accounts of the instance it came from: it arrives private.
            visibility=VISIBILITY_PUBLIC if raw.get("visibility") == VISIBILITY_PUBLIC else VISIBILITY_PRIVATE,
        ))
    if problems:
        raise ImportError_(problems)
    return watches


def export_yaml(watches: Iterable[Watch]) -> str:
    """Serialise watches. Channel routing, owners, the accounts a watch is shared with and
    share links are left out: they mean nothing on another instance."""
    items = []
    for w in watches:
        item: dict = {"name": w.name, "query": w.query, "match": w.match}
        optional = {
            "exclude": w.exclude or None,
            "min_price": w.min_price,
            "max_price": w.max_price,
            "deal_drop_pct": w.deal_drop_pct,
            "in_stock_only": w.in_stock_only,
            "notify_restock": w.notify_restock or None,
            "sites": list(w.sites),
            "urls": dict(w.site_urls) or None,
            "product_urls": [
                {"url": p.url, "selector": p.selector} if p.selector else p.url for p in w.product_urls
            ] or None,
            "enabled": None if w.enabled else False,
            "visibility": VISIBILITY_PUBLIC if w.visibility == VISIBILITY_PUBLIC else None,
        }
        item.update({k: v for k, v in optional.items() if v is not None})
        items.append(item)
    return yaml.safe_dump({"version": EXPORT_VERSION, "watches": items}, sort_keys=False, allow_unicode=True)
