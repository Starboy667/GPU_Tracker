"""Turn a ``Message`` into plain text, Markdown or HTML.

Wording is English, numbers use the French format ("1 079,95 €"). Product titles come from scraped
pages, so every renderer treats item text and URLs as hostile: HTML is escaped, Markdown specials
are neutralised, and only http(s) URLs are ever turned into links.
"""
from __future__ import annotations

import html
import re

from pricewatch.notify.base import KIND_DEAL, KIND_RESTOCK, AlertItem, Message
from pricewatch.notify.textutil import (
    ELLIPSIS,
    MAX_URL,
    chunk_blocks,
    clip,
    join_fitting,
    safe_url,
    truncate,
)

__all__ = [
    "APP_LINK_LABEL", "ELLIPSIS", "MAX_DETAIL", "MAX_HEADLINE", "MAX_TITLE", "MAX_URL",
    "as_html", "as_markdown", "as_text", "chunk_blocks", "clip", "first_url", "fmt_eur", "html_link",
    "item_headline", "item_html", "item_lines", "item_markdown", "item_text", "join_fitting",
    "md_escape", "md_line", "md_link", "price_context", "price_line", "safe_url", "stock_label", "truncate",
]

APP_LINK_LABEL = "Open Price Watch"
MAX_HEADLINE = 250
MAX_TITLE = 300
MAX_DETAIL = 1500

_STOCK_LABELS = {True: "In stock", False: "Out of stock", None: "Stock unknown"}
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>~|])")
_MD_BLOCK_START = re.compile(r"^(\s*)([#+-]|\d+(?=[.)]))")


# --------------------------------------------------------------------------- small helpers

def fmt_eur(price: float) -> str:
    return f"{price:,.2f} €".replace(",", " ").replace(".", ",")


def stock_label(in_stock: bool | None) -> str:
    return _STOCK_LABELS[in_stock]


def first_url(message: Message) -> str:
    """Best single link for channels that support one click target."""
    for item in message.items:
        if safe_url(item.url):
            return safe_url(item.url)
    return safe_url(message.app_url)


# --------------------------------------------------------------------------- item content

def price_context(item: AlertItem) -> str:
    """'(-12 % vs usual 1 049,00 €, median over 14 days)' or '' without a baseline."""
    if item.price is None or not item.baseline:
        return ""
    pct = (item.price / item.baseline - 1) * 100
    span = ""
    if item.baseline_days:
        span = f", median over {item.baseline_days} day{'s' if item.baseline_days != 1 else ''}"
    return f"({pct:+.0f} % vs usual {fmt_eur(item.baseline)}{span})"


def price_line(item: AlertItem) -> str:
    if item.price is None:
        return ""
    return f"{fmt_eur(item.price)} {price_context(item)}".strip()


def item_headline(item: AlertItem) -> str:
    if item.kind == KIND_DEAL:
        headline = " — ".join(part for part in (item.watch, item.site) if part) or item.title
    elif item.kind == KIND_RESTOCK:
        where = f" at {item.site}" if item.site else ""
        headline = f"{item.watch} — back in stock{where}"
    else:
        headline = item.title
    return clip(headline, MAX_HEADLINE)


def item_lines(item: AlertItem) -> tuple[str, ...]:
    """Plain-text body lines of one item, without its headline or link."""
    if item.kind == KIND_DEAL:
        lines = [clip(item.title, MAX_TITLE), price_line(item)]
        if item.target_price is not None:
            lines.append(f"Target: {fmt_eur(item.target_price)}")
        if item.previous_price is not None:
            lines.append(f"Last alert: {fmt_eur(item.previous_price)}")
        lines.append(stock_label(item.in_stock))
    elif item.kind == KIND_RESTOCK:
        lines = [clip(item.title, MAX_TITLE), fmt_eur(item.price) if item.price is not None else ""]
    else:
        detail = truncate((item.detail or "").strip(), MAX_DETAIL)
        lines = [" ".join(line.split()) for line in detail.splitlines()]
    return tuple(line for line in lines if line)


# --------------------------------------------------------------------------- plain text

def item_text(item: AlertItem) -> str:
    return "\n".join(part for part in (item_headline(item), *item_lines(item), safe_url(item.url)) if part)


def as_text(message: Message) -> str:
    blocks = [message.title, *(item_text(item) for item in message.items)]
    if safe_url(message.app_url):
        blocks.append(f"{APP_LINK_LABEL}: {safe_url(message.app_url)}")
    return "\n\n".join(block for block in blocks if block)


# --------------------------------------------------------------------------- markdown

def md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def md_line(text: str) -> str:
    """Escape one line, including a leading marker that would open a heading or a list."""
    escaped = md_escape(text)
    match = _MD_BLOCK_START.match(escaped)
    if not match:
        return escaped
    indent, marker = match.groups()
    rest = escaped[match.end():]
    if marker.isdigit():
        return f"{indent}{marker}\\{rest}"
    return f"{indent}\\{marker}{rest}"


def md_link(label: str, url: str) -> str:
    target = safe_url(url)
    if not target:
        return md_escape(label)
    for char, code in (("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        target = target.replace(char, code)
    return f"[{md_escape(label)}]({target})"


def item_markdown(item: AlertItem) -> str:
    lines = [f"**{md_link(item_headline(item), item.url)}**", *(md_line(line) for line in item_lines(item))]
    return "  \n".join(lines)  # two trailing spaces: hard line break


def as_markdown(message: Message, *, with_title: bool = True) -> str:
    blocks = [f"**{md_escape(message.title)}**"] if with_title and message.title else []
    blocks += [item_markdown(item) for item in message.items]
    if safe_url(message.app_url):
        blocks.append(md_link(APP_LINK_LABEL, message.app_url))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- HTML

def html_link(label: str, url: str) -> str:
    target = safe_url(url)
    text = html.escape(label)
    if not target:
        return text
    return f'<a href="{html.escape(target, quote=True)}">{text}</a>'


def item_html(item: AlertItem) -> str:
    """Chat-grade HTML: only <b> and <a>, one line per ``\\n`` (Telegram, Pushover)."""
    lines = [f"<b>{html_link(item_headline(item), item.url)}</b>", *(html.escape(line) for line in item_lines(item))]
    return "\n".join(lines)


def as_html(message: Message) -> str:
    """Document-grade HTML fragment for e-mail."""
    parts = [f"<h2>{html.escape(message.title)}</h2>"]
    parts += ["<p>" + item_html(item).replace("\n", "<br>\n") + "</p>" for item in message.items]
    if safe_url(message.app_url):
        parts.append(f"<p>{html_link(APP_LINK_LABEL, message.app_url)}</p>")
    return "\n".join(parts)
