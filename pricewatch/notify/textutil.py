"""Text primitives shared by the renderers and the channels: clipping, size limits, safe links."""
from __future__ import annotations

import re
from collections.abc import Sequence

ELLIPSIS = "…"
MAX_URL = 1500
_URL_RE = re.compile(r"https?://[^\s\x00-\x1f\x7f]+", re.IGNORECASE)


def clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut to ``limit`` characters on one line."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + ELLIPSIS


def truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, on a line boundary when there is one."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    head = text[: limit - 1]
    cut = head.rfind("\n")
    if cut > 0:
        return head[:cut].rstrip() + "\n" + ELLIPSIS
    return head.rstrip() + ELLIPSIS


def safe_url(url: str) -> str:
    """The URL if it is a plain http(s) link we are willing to render, else ''.

    Item URLs come from scraped pages: this keeps ``javascript:`` and friends out of every channel.
    """
    url = (url or "").strip()
    if len(url) > MAX_URL or not _URL_RE.fullmatch(url):
        return ""
    return url


def join_fitting(blocks: Sequence[str], limit: int, sep: str = "\n\n") -> str:
    """Join as many leading blocks as fit in ``limit``; mention how many were left out.

    Returns '' when not even the first block fits, so the caller can fall back to truncation.
    """
    for count in range(len(blocks), 0, -1):
        parts = list(blocks[:count])
        if count < len(blocks):
            parts.append(f"{ELLIPSIS} and {len(blocks) - count} more")
        text = sep.join(parts)
        if len(text) <= limit:
            return text
    return ""


def chunk_blocks(blocks: Sequence[str], limit: int, sep: str = "\n\n") -> list[str]:
    """Group blocks into messages of at most ``limit`` characters, never splitting a block
    (a single oversized block is truncated)."""
    chunks: list[str] = []
    current = ""
    for block in blocks:
        block = truncate(block, limit)
        candidate = f"{current}{sep}{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks
