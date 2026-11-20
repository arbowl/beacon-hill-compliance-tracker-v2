"""Content-addressed byte storage."""

from typing import Protocol


class BlobStore(Protocol):
    """Stores bytes under their sha256 hex digest."""

    def put(self, content: bytes) -> str: ...

    def get(self, content_hash: str) -> bytes: ...

    def exists(self, content_hash: str) -> bool: ...
