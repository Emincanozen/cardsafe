"""Manifest reading/writing and re-verification."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .hashing import hash_file, new_hasher

MANIFEST_NAME = "cardsafe-manifest.json"
REPORT_NAME = "cardsafe-report.txt"
PART_SUFFIX = ".cardsafe-part"
FORMAT_ID = "cardsafe-manifest"
FORMAT_VERSION = 1


def build_manifest(algorithm: str, source_name: str, files: list[dict]) -> dict:
    return {
        "format": FORMAT_ID,
        "version": FORMAT_VERSION,
        "tool": f"cardsafe {__version__}",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "algorithm": algorithm,
        "source_name": source_name,
        "files": sorted(files, key=lambda e: e["path"]),
    }


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + PART_SUFFIX)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_manifest(directory: Path) -> dict:
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"no {MANIFEST_NAME} in {directory}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("format") != FORMAT_ID or data.get("version") != FORMAT_VERSION:
        raise ValueError(f"unsupported manifest format in {path}")
    if not isinstance(data.get("files"), list) or not isinstance(data.get("algorithm"), str):
        raise ValueError(f"malformed manifest: {path}")
    new_hasher(data["algorithm"])  # ValueError if unknown/unavailable
    seen = set()
    for i, e in enumerate(data["files"]):
        if (not isinstance(e, dict) or not isinstance(e.get("path"), str)
                or not isinstance(e.get("hash"), str)
                or not isinstance(e.get("size"), int) or isinstance(e.get("size"), bool)):
            raise ValueError(f"malformed manifest entry #{i} in {path}")
        if e["path"] in seen:
            raise ValueError(f"duplicate path in manifest {path}: {e['path']}")
        seen.add(e["path"])
    return data


def _safe_join(root: Path, rel: str) -> Path | None:
    """Join a manifest path under root; None if it would escape root."""
    if not rel or rel.startswith("/") or "\\" in rel or ":" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return root.joinpath(*parts)


def _escapes_root(root: Path, rel: str, target: Path) -> bool:
    """True if any component is a symlink or the target resolves outside root."""
    cur = root
    for part in rel.split("/"):
        cur = cur / part
        if cur.is_symlink():
            return True
    try:
        return not target.resolve().is_relative_to(root.resolve())
    except OSError:
        return True


def verify_directory(directory, strict: bool = False, progress=None) -> dict:
    """Re-hash every file listed in the manifest. Returns a report dict.

    ``progress(bytes_done, bytes_total, relative_path)`` is called per chunk.
    """
    root = Path(directory)
    manifest = load_manifest(root)
    algorithm = manifest["algorithm"]
    total = sum(e["size"] for e in manifest["files"])
    done = [0]

    def tick(n):
        done[0] += n
        if progress:
            progress(done[0], total, label[0])

    label = [""]
    results = []
    listed = set()
    ok_count = 0
    for entry in manifest["files"]:
        rel = entry["path"]
        label[0] = rel
        listed.add(rel)
        target = _safe_join(root, rel)
        if target is None:
            status = "invalid-path"
        elif _escapes_root(root, rel, target):
            status = "symlink-or-outside-root"
        elif not target.is_file():
            status = "missing"
        else:
            try:
                if target.stat().st_size != entry["size"]:
                    status = "size-mismatch"
                else:
                    status = ("ok" if hash_file(target, algorithm, tick) == entry["hash"]
                              else "hash-mismatch")
            except OSError as exc:
                status = f"error: {exc}"
        if status == "ok":
            ok_count += 1
        results.append({"path": rel, "status": status})
    extras = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for d in list(dirnames):
            if (base / d).is_symlink():
                extras.append((base / d).relative_to(root).as_posix() + " (symlinked directory)")
                dirnames.remove(d)
        for name in sorted(filenames):
            rel = (base / name).relative_to(root).as_posix()
            if rel in listed or (dirpath == str(root) and name in (MANIFEST_NAME, REPORT_NAME)):
                continue
            extras.append(rel)
    failed = [r for r in results if r["status"] != "ok"]
    ok = not failed and not (strict and extras)
    return {
        "directory": str(root),
        "algorithm": algorithm,
        "ok": ok,
        "files_total": len(results),
        "files_ok": ok_count,
        "files_failed": len(failed),
        "extra_files": extras,
        "results": results,
    }
