"""Static asset versions (cache busting) and the service worker source.

- :func:`static_url` (Jinja global): ``static_url("css/app.css")`` -> ``/static/css/app.css?v=<hash>``, the hash
  being the first 12 hex digits of the file's SHA-256. A changed file gets a new URL, so browsers and the
  service worker never serve a stale copy; unchanged files stay cached across app releases.
- :func:`asset_version`: one hash over all static files (and the service worker template). It names the
  service worker cache (``deputat-shell-<version>``), so the SW is re-installed and re-precaches the shell
  whenever any asset changes.
- :func:`service_worker_source`: ``app/static/sw.js`` with ``__ASSET_VERSION__`` and the ``ASSET_HASHES``
  placeholder filled in; served by ``GET /sw.js`` in :mod:`app.main`.

Hashes are computed once per process (the image is immutable; ``reset_asset_cache()`` exists for tests).
Nothing is written to disk.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from app.config import PROJECT_ROOT

STATIC_DIR = PROJECT_ROOT / "app" / "static"
SW_FILE = STATIC_DIR / "sw.js"
HASH_LEN = 12

SW_VERSION_PLACEHOLDER = "__ASSET_VERSION__"
SW_HASHES_PLACEHOLDER = "const ASSET_HASHES = {};"


@lru_cache(maxsize=1)
def asset_hashes() -> dict[str, str]:
    """``{"css/app.css": "<12 hex>", ...}`` for every file below app/static (POSIX paths)."""
    result: dict[str, str] = {}
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            rel = path.relative_to(STATIC_DIR).as_posix()
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()[:HASH_LEN]
    return result


@lru_cache(maxsize=1)
def asset_version() -> str:
    """Combined hash of all static files (changes when any asset changes)."""
    digest = hashlib.sha256()
    for rel, value in asset_hashes().items():
        digest.update(f"{rel}={value}\n".encode())
    return digest.hexdigest()[:HASH_LEN]


def static_url(path: str) -> str:
    """URL of a static file with its content hash as cache-busting query (unknown files: plain URL)."""
    rel = path.lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    value = asset_hashes().get(rel)
    return f"/static/{rel}?v={value}" if value else f"/static/{rel}"


@lru_cache(maxsize=1)
def service_worker_source() -> str:
    """sw.js with the asset version and the per-file hashes injected."""
    source = SW_FILE.read_text(encoding="utf-8")
    hashes = {f"/static/{rel}": value for rel, value in asset_hashes().items() if rel != "sw.js"}
    source = source.replace(SW_VERSION_PLACEHOLDER, asset_version())
    return source.replace(
        SW_HASHES_PLACEHOLDER,
        "const ASSET_HASHES = " + json.dumps(hashes, sort_keys=True, separators=(",", ":")) + ";",
    )


def reset_asset_cache() -> None:
    asset_hashes.cache_clear()
    asset_version.cache_clear()
    service_worker_source.cache_clear()
