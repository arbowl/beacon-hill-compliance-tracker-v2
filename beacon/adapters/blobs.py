"""Content-addressed blob files on disk."""

import gzip
import hashlib
import os
from pathlib import Path

_GZIP_MAGIC = bytes((0x1F, 0x8B))


class FileBlobStore:
    """Stores gzip-compressed bytes at root/ab/cd/<sha256 of the raw bytes>.

    Writes are atomic and idempotent; uncompressed legacy blobs are still readable.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, content_hash: str) -> Path:
        return self._root / content_hash[:2] / content_hash[2:4] / content_hash

    def put(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        path = self._path(content_hash)
        if path.exists():
            return content_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(gzip.compress(content))
        os.replace(tmp, path)
        return content_hash

    def get(self, content_hash: str) -> bytes:
        raw = self._path(content_hash).read_bytes()
        return gzip.decompress(raw) if raw[:2] == _GZIP_MAGIC else raw

    def exists(self, content_hash: str) -> bool:
        return self._path(content_hash).exists()
