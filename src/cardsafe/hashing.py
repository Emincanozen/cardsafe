"""Checksum algorithms and streaming helpers."""

from __future__ import annotations

import hashlib
from typing import Callable

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

_ALGORITHMS: dict[str, Callable[[], object]] = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
    "md5": hashlib.md5,
    "blake2b": hashlib.blake2b,
}

try:  # optional, fast non-cryptographic hash
    import xxhash  # type: ignore

    _ALGORITHMS["xxh64"] = xxhash.xxh64
except ImportError:  # pragma: no cover - depends on environment
    pass

DEFAULT_ALGORITHM = "sha256"


def available_algorithms() -> list[str]:
    return sorted(_ALGORITHMS)


def new_hasher(name: str):
    try:
        return _ALGORITHMS[name]()
    except KeyError:
        extra = " (install with: pip install cardsafe[xxhash])" if name == "xxh64" else ""
        raise ValueError(
            f"unsupported or unavailable algorithm {name!r}{extra}; "
            f"available: {', '.join(available_algorithms())}"
        ) from None


def hash_file(path, algorithm: str, progress=None) -> str:
    """Hash a file by reading it fully from disk.

    ``progress(n_bytes)`` is called after each chunk, if given."""
    h = new_hasher(algorithm)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            if progress:
                progress(len(chunk))
    return h.hexdigest()
