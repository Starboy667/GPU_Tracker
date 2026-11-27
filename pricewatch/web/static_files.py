"""Serving the static folder: addresses that can be cached for good, text sent compressed."""
from __future__ import annotations

import gzip
import hashlib
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# /static/v-<hash>/app.css is /static/app.css under a name that changes with every release, so
# browsers can keep it forever. Scripts and stylesheets reference each other with relative
# addresses, which keeps the whole module graph and the fonts under the same versioned name.
VERSION_SEGMENT = re.compile(r"^/v-([0-9a-f]{1,64})(?=/)")
IMMUTABLE = "public, max-age=31536000, immutable"
COMPRESSIBLE = (".js", ".css", ".svg", ".json", ".txt")  # fonts and images already are
MIN_COMPRESSED_SIZE = 1024


def static_version(directory: Path) -> str:
    """Short hash of every static file, subfolders included: changes whenever one of them does."""
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


@dataclass(frozen=True)
class Packed:
    """One text file, compressed."""

    body: bytes
    media_type: str
    etag: str


def pack(directory: Path) -> Mapping[str, Packed]:
    """Every text file of the folder worth compressing, compressed once, by its address under
    /static. Done at startup: compressing on each request would let anybody keep a processor
    busy by asking for the largest file in a loop."""
    packed = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix in COMPRESSIBLE):
        raw = path.read_bytes()
        if len(raw) < MIN_COMPRESSED_SIZE:
            continue
        body = gzip.compress(raw, compresslevel=9, mtime=0)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if media_type.startswith("text/"):
            media_type += "; charset=utf-8"
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        packed["/" + path.relative_to(directory).as_posix()] = Packed(body, media_type, etag)
    return packed


def accepts_gzip(header: str) -> bool:
    """Whether an Accept-Encoding value allows gzip ("gzip;q=0" refuses it)."""
    for part in header.lower().split(","):
        name, _, params = part.strip().partition(";")
        if name.strip() != "gzip":
            continue
        quality = params.strip().removeprefix("q=").strip()
        try:
            return not quality or float(quality) > 0
        except ValueError:
            return False
    return False


class StaticAssets:
    """The app mounted on /static.

    Only these files are compressed. Pages are not: compressing a response that mixes a secret
    (the CSRF token) with text an attacker can choose leaks the secret through its size.
    """

    def __init__(self, directory: Path):
        self._files = StaticFiles(directory=str(directory))
        self._packed = pack(directory)
        self._version = static_version(directory)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self._files(scope, receive, send)
        root = scope.get("root_path", "")
        full_path = scope["path"]
        route_path = full_path[len(root):] if full_path.startswith(root) else full_path
        # Kept for good only under this release's own name. A page from the previous release
        # still gets today's file under yesterday's name, but nobody pins it there for a year.
        pinned = False
        versioned = VERSION_SEGMENT.match(route_path)
        if versioned:
            pinned = versioned.group(1) == self._version
            route_path = route_path[versioned.end():]
            scope = {**scope, "path": root + route_path, "raw_path": (root + route_path).encode()}

        headers = Headers(scope=scope)
        packed = self._packed.get(route_path)  # only names of real files are in there
        if (
            packed is not None
            and scope["method"] in ("GET", "HEAD")
            and "range" not in headers  # a range is a slice of the file, not of its compressed form
            and accepts_gzip(headers.get("accept-encoding", ""))
        ):
            return await self._send_packed(packed, pinned, scope, receive, send)
        if not pinned:
            return await self._files(scope, receive, send)

        async def send_pinned(message):
            if message["type"] == "http.response.start" and message["status"] in (200, 304):
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*kept, (b"cache-control", IMMUTABLE.encode())]}
            await send(message)

        await self._files(scope, receive, send_pinned)

    @staticmethod
    async def _send_packed(packed: Packed, pinned: bool, scope, receive, send) -> None:
        headers = {"etag": packed.etag, "vary": "Accept-Encoding"}
        if pinned:
            headers["cache-control"] = IMMUTABLE
        known = Headers(scope=scope).get("if-none-match", "")
        if packed.etag in (tag.strip() for tag in known.split(",")):
            return await Response(status_code=304, headers=headers)(scope, receive, send)
        headers["content-encoding"] = "gzip"
        headers["content-length"] = str(len(packed.body))
        body = b"" if scope["method"] == "HEAD" else packed.body
        await Response(body, media_type=packed.media_type, headers=headers)(scope, receive, send)
