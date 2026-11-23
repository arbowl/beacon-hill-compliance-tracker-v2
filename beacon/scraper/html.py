"""One parse per page body, shared by the extractors that read the same page."""

from functools import lru_cache

from bs4 import BeautifulSoup


@lru_cache(maxsize=4)
def parse_html(body: bytes) -> BeautifulSoup:
    """The parsed page, reused while the same body is read again.

    Callers must treat the tree as read-only: it is shared between them.
    """
    return BeautifulSoup(body, "html.parser")
