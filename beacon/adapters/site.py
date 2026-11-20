"""Knowledge of malegislature.gov page quirks."""

import hashlib
import re

_VOLATILE = (
    re.compile(rb'<input name="__RequestVerificationToken"[^>]*>'),
    re.compile(rb"<div[^>]*\bweatherBox\b[^>]*>.*?</div>", re.S),
)


def normalized_hash(body: bytes) -> str:
    """sha256 of the body with per-request noise (tokens, weather box) removed."""
    for pattern in _VOLATILE:
        body = pattern.sub(b"", body)
    return hashlib.sha256(body).hexdigest()
