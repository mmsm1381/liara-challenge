"""Template context shared by every page."""

import hashlib
from functools import lru_cache
from pathlib import Path

ASSETS = ("assistant/app.css", "assistant/chat.js")


@lru_cache(maxsize=1)
def _asset_version():
    # Changes whenever the CSS or JS changes, so browsers never keep a stale copy.
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parent / "static"
    for name in ASSETS:
        try:
            digest.update((root / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:10]


def assets(request):
    return {"asset_version": _asset_version()}
