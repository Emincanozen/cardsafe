"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .copier import DEFAULT_FOLDER_TEMPLATE, CardsafeError, copy_card
from .hashing import DEFAULT_ALGORITHM, available_algorithms
from .manifest import verify_directory

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class Bar:
    """Minimal single-line progress bar on stderr (only when it is a TTY)."""

    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stderr.isatty()
        self.last = -1

    def __call__(self, done: int, total: int, label: str = "") -> None:
        if not self.enabled:
            return
        pct = 100 if total == 0 else int(done * 100 / total)
        if pct == self.last:
            return
        self.last = pct
        width = 30
        fill = width * pct // 100
        label = label[-30:]
        sys.stderr.write(f"\r[{'#' * fill}{'.' * (width - fill)}] {pct:3d}%  {label:<30}")
        sys.stderr.flush()

    def close(self) -> None:
        if self.enabled:
            sys.stderr.write("\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cardsafe", description="Verified offload of camera cards.")
    p.add_argument("--version", action="version", version=f"cardsafe {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("copy", help="copy a source folder to one or more destinations and verify")
    c.add_argument("source")
    c.add_argument("dest", nargs="+", help="destination base folder(s)")
    c.add_argument("--algo", default=DEFAULT_ALGORITHM, choices=available_algorithms(),
                   help=f"checksum algorithm (default: {DEFAULT_ALGORITHM}; xxh64 needs the optional xxhash package)")
    c.add_argument("--folder", default=DEFAULT_FOLDER_TEMPLATE,
                   help="sub-folder template inside each destination; fields {date} {time} {datetime} {name}; "
                        f"empty string copies straight into the destination (default: {DEFAULT_FOLDER_TEMPLATE})")
    c.add_argument("--force", action="store_true", help="overwrite destination files whose content differs")
    c.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
    c.add_argument("--json", action="store_true", help="print the report as JSON on stdout")
    c.add_argument("--quiet", action="store_true", help="no progress bar; human summary only on failure")

    v = sub.add_parser("verify", help="re-verify a copied folder against its manifest")
    v.add_argument("dir")
    v.add_argument("--strict", action="store_true", help="also fail when files not in the manifest are present")
    v.add_argument("--json", action="store_true")
    v.add_argument("--quiet", action="store_true", help="no progress bar; print only on failure")
    return p


def _print_copy(report: dict) -> None:
    mode = "DRY RUN - " if report["dry_run"] else ""
    print(f"{mode}{report['files']} file(s), {report['bytes']} bytes, algorithm {report['algorithm']}")
    for fr in report["file_results"]:
        for dest, d in fr["destinations"].items():
            detail = f"  ({d['detail']})" if d.get("detail") else ""
            print(f"  {d['status']:<22} {fr['path']} -> {dest}{detail}")
    for s in report["skipped_source_entries"]:
        print(f"  not copied             {s['path']}  ({s['reason']})")
    for s in report["renamed_source_files"]:
        print(f"  renamed                {s['path']} -> {s['copied_as']}")
    for e in report["errors"]:
        print(f"  error: {e}")
    counts = ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())) or "nothing to do"
    print(f"{'OK' if report['ok'] else 'FAILED'}: {counts}")


def _print_verify(report: dict) -> None:
    for r in report["results"]:
        if r["status"] != "ok":
            print(f"  {r['status']:<14} {r['path']}")
    for e in report["extra_files"]:
        print(f"  not in manifest {e}")
    print(f"{'OK' if report['ok'] else 'FAILED'}: {report['files_ok']}/{report['files_total']} files verified "
          f"({report['algorithm']})")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    bar = Bar(enabled=not (args.quiet or getattr(args, "json", False)))
    try:
        if args.command == "copy":
            report = copy_card(args.source, args.dest, algorithm=args.algo, folder=args.folder,
                               force=args.force, dry_run=args.dry_run, progress=bar)
            bar.close()
            if args.json:
                print(json.dumps(report, indent=2))
            elif not args.quiet or not report["ok"]:
                _print_copy(report)
            return EXIT_OK if report["ok"] else EXIT_FAILED
        report = verify_directory(args.dir, strict=args.strict, progress=bar)
        bar.close()
        if args.json:
            print(json.dumps(report, indent=2))
        elif not args.quiet or not report["ok"]:
            _print_verify(report)
        return EXIT_OK if report["ok"] else EXIT_FAILED
    except (CardsafeError, ValueError, OSError) as exc:
        bar.close()
        print(f"cardsafe: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        bar.close()
        print("cardsafe: interrupted; re-run the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
