"""Core offload engine: one read of the source, N destinations, re-read to verify.

The source is only ever opened read-only. Nothing in this module deletes,
moves or modifies anything under the source directory.
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import manifest as mf
from .hashing import CHUNK_SIZE, DEFAULT_ALGORITHM, hash_file, new_hasher

# Test seam: called as hook(part_path) after a copy is written and before it is
# re-read for verification. Tests use it to simulate bit rot on the destination.
_after_write_hook = None

DEFAULT_FOLDER_TEMPLATE = "{date}_{name}"

_IS_WINDOWS = os.name == "nt"
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


class CardsafeError(Exception):
    """Fatal usage/setup error (bad paths, bad template)."""


def invalid_windows_name(component: str) -> str | None:
    """Reason why ``component`` cannot be a file name on Windows, else None."""
    if re.search(r'[<>:"|?*\x00-\x1f]', component):
        return "contains characters not allowed on Windows"
    if component != component.rstrip(" ."):
        return "ends with a space or dot"
    if component.split(".")[0].upper() in _WIN_RESERVED:
        return "is a reserved Windows device name"
    return None


def folder_parts(template: str, source: Path, now: datetime | None = None) -> list[str]:
    """Expand the destination folder template into safe path components."""
    now = now or datetime.now()
    resolved = source.resolve()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", resolved.name or "card").strip(" .") or "card"
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    try:
        text = template.format(
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H%M%S"),
            datetime=now.strftime("%Y-%m-%d_%H%M%S"),
            name=name,
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise CardsafeError(
            f"invalid --folder template {template!r} (use {{date}}, {{time}}, {{datetime}}, {{name}}): {exc}"
        ) from None
    parts = [p for p in re.split(r"[/\\]", text) if p]
    if any(p in (".", "..") or ":" in p for p in parts):
        raise CardsafeError(f"folder template expands to an unsafe path: {text!r}")
    if _IS_WINDOWS:
        for p in parts:
            why = invalid_windows_name(p)
            if why:
                raise CardsafeError(f"folder name {p!r} {why}")
    return parts


def invalid_windows_name_path(rel: str) -> str | None:
    for comp in rel.split("/"):
        why = invalid_windows_name(comp)
        if why:
            return f"{comp!r} {why}"
    return None


def _scan_source(src: Path):
    files, skipped, renamed = [], [], []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames.sort()
        base = Path(dirpath)
        for d in list(dirnames):
            if (base / d).is_symlink():
                skipped.append({"path": (base / d).relative_to(src).as_posix(), "reason": "symlink"})
                dirnames.remove(d)
        for name in sorted(filenames):
            full = base / name
            rel = full.relative_to(src).as_posix()
            if full.is_symlink() or not full.is_file():
                skipped.append({"path": rel, "reason": "not a regular file"})
                continue
            if name.endswith(mf.PART_SUFFIX):
                skipped.append({"path": rel, "reason": "partial-copy leftover"})
                continue
            try:
                size = full.stat().st_size
            except OSError as exc:
                skipped.append({"path": rel, "reason": f"unreadable: {exc}", "fatal": True})
                continue
            dest_rel = rel
            if rel in (mf.MANIFEST_NAME, mf.REPORT_NAME):
                # would be clobbered by our own metadata files: keep the data under another name
                dest_rel = rel + ".source"
                renamed.append({"path": rel, "copied_as": dest_rel})
            files.append((full, dest_rel, size))
    return files, skipped, renamed


def _safe_close(handle) -> str | None:
    try:
        handle.close()
    except OSError as exc:
        return str(exc)
    return None


def _stream_copy(srcp: Path, size: int, targets: list[Path], algorithm: str, pool, tick):
    """Read source once; hash it; fan chunks out to all part files.

    A failing destination is dropped (its error recorded) without affecting the others.
    Returns (digest, {part_path: error_or_None}).
    """
    hasher = new_hasher(algorithm)
    handles: dict[Path, object] = {}
    errors: dict[Path, str | None] = {}
    for part in targets:
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            handles[part] = open(part, "wb")
            errors[part] = None
        except OSError as exc:
            errors[part] = f"cannot create: {exc}"
    total = 0
    try:
        with open(srcp, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                hasher.update(chunk)
                futures = {part: pool.submit(h.write, chunk) for part, h in handles.items()}
                for part, fut in futures.items():
                    try:
                        fut.result()
                    except OSError as exc:
                        errors[part] = f"write failed: {exc}"
                for part in [p for p, e in errors.items() if e and p in handles]:
                    _safe_close(handles.pop(part))
                tick(len(chunk))
    finally:
        for part, h in handles.items():
            try:
                h.flush()
                os.fsync(h.fileno())
            except OSError as exc:
                errors[part] = f"flush failed: {exc}"
            msg = _safe_close(h)
            if msg and not errors[part]:
                errors[part] = f"close failed: {msg}"
    if total != size:
        for part in errors:
            errors[part] = errors[part] or "source changed while reading (size differs from scan)"
    return hasher.hexdigest(), errors


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _exact_name_exists(path: Path, cache: dict) -> bool:
    """False when ``path`` only 'exists' through case-insensitive matching of another name."""
    names = cache.get(path.parent)
    if names is None:
        try:
            names = set(os.listdir(path.parent))
        except OSError:
            names = set()
        cache[path.parent] = names
    return path.name in names


def _safe_hash(path: Path, algorithm: str, tick=None):
    try:
        return hash_file(path, algorithm, tick)
    except OSError:
        return None


def _finish_parts(parts_map, errs, finals, srcp, digest, algorithm, pool, statuses, ok_status, tick=None):
    """Re-read every written part file, compare, then move into place."""
    live = [r for r, p in parts_map.items() if errs.get(p) is None]
    for r, p in parts_map.items():
        if errs.get(p) is not None:
            statuses[r] = ("error", errs[p])
            _discard(p)
    if _after_write_hook:
        for r in live:
            _after_write_hook(parts_map[r])
    hashes = list(pool.map(lambda r: _safe_hash(parts_map[r], algorithm, tick), live))
    for r, h in zip(live, hashes):
        p = parts_map[r]
        if h != digest:
            statuses[r] = ("failed", "verification failed: destination checksum differs from source")
            _discard(p)
            continue
        try:
            st = srcp.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(p, finals[r])
            statuses[r] = (ok_status, None)
        except OSError as exc:
            statuses[r] = ("error", f"cannot finalise: {exc}")
            _discard(p)


def copy_card(
    src,
    dests,
    algorithm: str = DEFAULT_ALGORITHM,
    folder: str = DEFAULT_FOLDER_TEMPLATE,
    force: bool = False,
    dry_run: bool = False,
    progress=None,
    now: datetime | None = None,
) -> dict:
    """Copy ``src`` into every destination, verify, and write manifests.

    ``progress(bytes_done, bytes_total, relative_path)`` is called while running; the
    total covers one source read plus one verification read per destination.
    Returns a JSON-serialisable report; ``report["ok"]`` is the success flag.
    """
    new_hasher(algorithm)  # validate early
    src = Path(src)
    if not src.is_dir():
        raise CardsafeError(f"source is not a directory: {src}")
    src_real = src.resolve()
    if not dests:
        raise CardsafeError("at least one destination is required")
    parts = folder_parts(folder, src, now)
    roots: list[Path] = []
    for d in dests:
        root = Path(d).joinpath(*parts).resolve()  # resolve after joining: catches links in the folder part
        if root == src_real or src_real in root.parents:
            raise CardsafeError(f"destination {root} is inside the source; refusing")
        if root in src_real.parents:
            raise CardsafeError(f"source is inside destination folder {root}; refusing")
        if root in roots:
            raise CardsafeError(f"duplicate destination: {root}")
        roots.append(root)

    files, skipped, renamed = _scan_source(src_real)
    total_bytes = sum(s for _, _, s in files)
    work_total = total_bytes * (1 + len(roots))
    done = {"bytes": 0, "end": 0}
    lock = threading.Lock()
    current = {"rel": ""}

    def tick(n: int) -> None:
        with lock:
            done["bytes"] = min(done["bytes"] + n, done["end"])
            if progress:
                progress(done["bytes"], work_total, current["rel"])

    file_reports = []
    entries: dict[Path, list[dict]] = {r: [] for r in roots}
    errors: list[str] = [f"{s['path']}: {s['reason']}" for s in skipped if s.get("fatal")]
    dir_cache: dict = {}

    pool = ThreadPoolExecutor(max_workers=max(2, len(roots)))
    try:
        for srcp, rel, size in files:
            current["rel"] = rel
            done["end"] = done["bytes"] + size * (1 + len(roots))
            statuses: dict[Path, tuple[str, str | None]] = {}
            digest = None
            finals = {r: r.joinpath(*rel.split("/")) for r in roots}
            part_names = [p.with_name(p.name + mf.PART_SUFFIX) for p in finals.values()]

            missing, existing = [], []
            bad_name = invalid_windows_name_path(rel) if _IS_WINDOWS else None
            for r, p in finals.items():
                if bad_name:
                    statuses[r] = ("error", f"file name not valid on Windows: {bad_name}")
                elif os.path.lexists(p):
                    if not _exact_name_exists(p, dir_cache):
                        statuses[r] = ("error", "case-insensitive name collision with a different existing file")
                    elif p.is_file() and not p.is_symlink():
                        existing.append(r)
                    else:
                        statuses[r] = ("error", "destination exists and is not a regular file")
                else:
                    missing.append(r)

            if dry_run:
                for r in missing:
                    statuses[r] = ("would-copy", None)
                for r in existing:
                    if finals[r].stat().st_size == size:
                        statuses[r] = ("would-verify-existing", None)
                    else:
                        statuses[r] = ("would-overwrite", None) if force else ("conflict", "differs from source (size)")
                done["bytes"] = done["end"]
                file_reports.append(_file_report(rel, size, None, roots, statuses))
                continue

            try:
                rewrite: list[Path] = []
                hash_existing: list[Path] = []
                for r in existing:
                    if finals[r].stat().st_size != size:
                        if force:
                            rewrite.append(r)
                        else:
                            statuses[r] = ("conflict", "exists with different content (size); use --force")
                    else:
                        hash_existing.append(r)

                # Pass 1: single read of the source, writing every missing destination.
                parts_map = {r: finals[r].with_name(finals[r].name + mf.PART_SUFFIX) for r in missing}
                if missing or rewrite or hash_existing:
                    try:
                        digest, errs = _stream_copy(srcp, size, list(parts_map.values()), algorithm, pool, tick)
                    except OSError as exc:
                        for r in missing + rewrite + hash_existing:
                            statuses[r] = ("error", f"cannot read source: {exc}")
                        digest, errs = None, {}
                        missing, rewrite, hash_existing = [], [], []
                        parts_map = {}
                    if digest is not None:
                        _finish_parts(parts_map, errs, finals, srcp, digest, algorithm, pool, statuses,
                                      "copied", tick)

                # Existing, same-size destinations: identical (resume) or conflicting.
                if digest is not None and hash_existing:
                    hashes = pool.map(lambda x: _safe_hash(finals[x], algorithm, tick), hash_existing)
                    for r, h in zip(hash_existing, hashes):
                        if h == digest:
                            statuses[r] = ("skipped", "already present and identical")
                        elif force:
                            rewrite.append(r)
                        else:
                            statuses[r] = ("conflict", "exists with different content; use --force")

                # Pass 2: forced overwrite of differing files (reads this source file again).
                if digest is not None and rewrite:
                    rparts = {r: finals[r].with_name(finals[r].name + mf.PART_SUFFIX) for r in rewrite}
                    try:
                        d2, errs2 = _stream_copy(srcp, size, list(rparts.values()), algorithm, pool,
                                                 lambda n: None)
                    except OSError as exc:
                        for r in rewrite:
                            statuses[r] = ("error", f"cannot read source: {exc}")
                    else:
                        if d2 != digest:
                            for r in rewrite:
                                statuses[r] = ("error", "source changed while copying")
                        else:
                            _finish_parts(rparts, errs2, finals, srcp, digest, algorithm, pool, statuses,
                                          "overwritten", tick)

                try:
                    mtime = int(srcp.stat().st_mtime)
                except OSError:
                    mtime = None
                for r, (st, _msg) in statuses.items():
                    if st in ("copied", "skipped", "overwritten"):
                        entries[r].append({"path": rel, "size": size, "hash": digest, "mtime": mtime})
            finally:
                # never leave partial files behind (including on Ctrl+C)
                for p in part_names:
                    _discard(p)
            done["bytes"] = done["end"]
            if progress:
                progress(done["bytes"], work_total, rel)
            file_reports.append(_file_report(rel, size, digest, roots, statuses))
    finally:
        pool.shutdown(wait=True)

    report = _summarise(src_real, roots, algorithm, file_reports, skipped, dry_run, total_bytes)
    report["renamed_source_files"] = renamed
    report["errors"] = errors
    if errors:
        report["ok"] = False
    if not dry_run:
        for r in roots:
            try:
                r.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report["ok"] = False
                report["errors"].append(f"cannot create destination {r}: {exc}")
                continue
            merged = {}
            try:  # keep entries of an earlier run that this run did not (re)verify
                old = mf.load_manifest(r)
                if old["algorithm"] == algorithm:
                    merged = {e["path"]: e for e in old["files"]}
            except (OSError, ValueError):
                pass
            for e in entries[r]:
                merged[e["path"]] = e
            manifest = mf.build_manifest(algorithm, src_real.name or str(src_real), list(merged.values()))
            try:
                mf.write_json_atomic(r / mf.MANIFEST_NAME, manifest)
                (r / mf.REPORT_NAME).write_text(render_report(report, r), encoding="utf-8", newline="\n")
            except OSError as exc:
                report["ok"] = False
                report["errors"].append(f"could not write manifest/report in {r}: {exc}")
    return report


def _file_report(rel, size, digest, roots, statuses):
    return {
        "path": rel,
        "size": size,
        "hash": digest,
        "destinations": {
            str(r): {"status": statuses[r][0], **({"detail": statuses[r][1]} if statuses[r][1] else {})}
            for r in roots
            if r in statuses
        },
    }


_BAD = {"failed", "error", "conflict"}


def _summarise(src, roots, algorithm, file_reports, skipped, dry_run, total_bytes):
    counts: dict[str, int] = {}
    for fr in file_reports:
        for d in fr["destinations"].values():
            counts[d["status"]] = counts.get(d["status"], 0) + 1
    bad = sum(v for k, v in counts.items() if k in _BAD)
    return {
        "source": str(src),
        "destinations": [str(r) for r in roots],
        "algorithm": algorithm,
        "dry_run": dry_run,
        "ok": bad == 0,
        "files": len(file_reports),
        "bytes": total_bytes,
        "counts": counts,
        "skipped_source_entries": skipped,
        "renamed_source_files": [],
        "errors": [],
        "file_results": file_reports,
    }


def render_report(report: dict, root: Path) -> str:
    lines = [
        "cardsafe offload report",
        f"destination: {root}",
        f"source:      {report['source']}",
        f"algorithm:   {report['algorithm']}",
        f"files:       {report['files']}  ({report['bytes']} bytes)",
        f"result:      {'OK' if report['ok'] else 'FAILED'}",
        "counts (all destinations): "
        + (", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())) or "none"),
        "",
    ]
    for fr in report["file_results"]:
        d = fr["destinations"].get(str(root))
        if d is None:
            continue
        extra = f"  ({d['detail']})" if d.get("detail") else ""
        lines.append(f"{d['status']:<12} {fr['path']}{extra}")
    for s in report["skipped_source_entries"]:
        lines.append(f"not copied   {s['path']}  ({s['reason']})")
    for s in report.get("renamed_source_files", []):
        lines.append(f"renamed      {s['path']} -> {s['copied_as']}  (name reserved by cardsafe)")
    for e in report["errors"]:
        lines.append(f"error: {e}")
    return "\n".join(lines) + "\n"
