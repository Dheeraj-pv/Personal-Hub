from __future__ import annotations

import hmac

from starlette.requests import Request

_PREFIX = "Bearer "


def is_authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith(_PREFIX):
        return False
    # Compare bytes: compare_digest raises on non-ASCII str, and constant time
    # avoids leaking how many leading characters matched.
    return hmac.compare_digest(
        header[len(_PREFIX):].encode("utf-8", "surrogateescape"),
        token.encode("utf-8"),
    )
