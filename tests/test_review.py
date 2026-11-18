"""Regression tests for findings of an independent review."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardsafe import cli, copier, hashing, manifest
from test_cardsafe import Base, snapshot


def try_symlink(link: Path, target: Path, is_dir=False):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        raise unittest.SkipTest("cannot create symlinks here")


class VerifySymlinkTests(Base):
    def test_verify_does_not_follow_symlinks_out_of_root(self):
        self.run_copy()
        out = self.d1 / "card"
        outside = self.tmp / "outside"
        outside.mkdir()
        # symlinked file replacing a listed file, with identical content
        victim = out / "PRIVATE/CLIP.MP4"
        (outside / "CLIP.MP4").write_bytes(self.files["PRIVATE/CLIP.MP4"])
        victim.unlink()
        try_symlink(victim, outside / "CLIP.MP4")
        # symlinked parent directory
        real = out / "DCIM"
        moved = outside / "DCIM"
        real.rename(moved)
        try_symlink(real, moved, is_dir=True)
        rep = manifest.verify_directory(out)
        self.assertFalse(rep["ok"])
        status = {r["path"]: r["status"] for r in rep["results"]}
        self.assertEqual(status["PRIVATE/CLIP.MP4"], "symlink-or-outside-root")
        self.assertEqual(status["DCIM/100CANON/IMG_0001.CR3"], "symlink-or-outside-root")


class ReservedNamesTests(Base):
    def test_source_named_like_manifest_is_not_lost(self):
        (self.src / manifest.MANIFEST_NAME).write_text("{\"from\": \"card\"}")
        (self.src / manifest.REPORT_NAME).write_text("card report")
        report = self.run_copy()
        self.assertTrue(report["ok"], report)
        out = self.d1 / "card"
        self.assertEqual((out / (manifest.MANIFEST_NAME + ".source")).read_text(), "{\"from\": \"card\"}")
        self.assertEqual(len(report["renamed_source_files"]), 2)
        self.assertTrue(manifest.verify_directory(out, strict=True)["ok"])


class FailureIsolationTests(Base):
    def test_empty_card_unreachable_destination_fails(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        blocker = self.tmp / "blocker"
        blocker.write_text("i am a file")
        report = copier.copy_card(empty, [blocker / "sub"], folder="card")
        self.assertFalse(report["ok"])
        self.assertTrue(report["errors"])
        code = cli.main(["copy", str(empty), str(blocker / "sub"), "--folder", "card", "--quiet"])
        self.assertEqual(code, 1)

    def test_failing_destination_close_does_not_abort_healthy_one(self):
        real_open = open

        class Broken:
            def write(self, data):
                raise OSError(28, "No space left on device")

            def flush(self):
                raise OSError(28, "No space left on device")

            def fileno(self):
                return 0

            def close(self):
                raise OSError(28, "No space left on device")

        def fake_open(path, *a, **k):
            if str(path).endswith(manifest.PART_SUFFIX) and "disk2" in str(path):
                return Broken()
            return real_open(path, *a, **k)

        copier.open = fake_open
        try:
            report = self.run_copy()
        finally:
            del copier.open
        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"]["error"], len(self.files))
        self.assertEqual(report["counts"]["copied"], len(self.files))
        self.assertTrue(manifest.verify_directory(self.d1 / "card")["ok"])
        self.assertFalse(list(self.d1.rglob("*" + manifest.PART_SUFFIX)))

    def test_force_second_pass_read_error_is_handled(self):
        self.run_copy()
        target = self.d1 / "card/DCIM/100CANON/IMG_0001.CR3"
        target.write_bytes(b"B" * 5000)
        real_open = open
        src_file = str(self.src / "DCIM/100CANON/IMG_0001.CR3")
        calls = {"n": 0}

        def fake_open(path, *a, **k):
            if str(path) == src_file or str(path).endswith("IMG_0001.CR3") and "EOS_R5" in str(path):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError(5, "read error")
            return real_open(path, *a, **k)

        copier.open = fake_open
        try:
            report = self.run_copy(force=True)
        finally:
            del copier.open
        self.assertEqual(calls["n"], 2)  # documents: --force re-reads the source file
        self.assertFalse(report["ok"])
        self.assertEqual(target.read_bytes(), b"B" * 5000)
        self.assertFalse(list(self.d1.rglob("*" + manifest.PART_SUFFIX)))


class ManifestPreservationTests(Base):
    def test_conflict_run_does_not_truncate_manifest(self):
        self.run_copy()
        out = self.d1 / "card"
        (out / "EMPTY.TXT").write_bytes(b"x")  # conflict on re-run
        report = self.run_copy()
        self.assertFalse(report["ok"])
        for d in (self.d1, self.d2):
            m = json.loads((d / "card" / manifest.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(len(m["files"]), len(self.files))


class InterruptTests(Base):
    def test_keyboard_interrupt_leaves_no_part_files(self):
        def boom(part):
            raise KeyboardInterrupt

        copier._after_write_hook = boom
        with self.assertRaises(KeyboardInterrupt):
            self.run_copy()
        self.assertFalse(list(self.tmp.rglob("*" + manifest.PART_SUFFIX)))
        with mock.patch("cardsafe.cli.copy_card", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["copy", str(self.src), str(self.d1), "--quiet"]), 130)

    def test_strict_verify_reports_part_files(self):
        self.run_copy()
        out = self.d1 / "card"
        (out / "DCIM" / ("x.bin" + manifest.PART_SUFFIX)).write_bytes(b"junk")
        self.assertFalse(manifest.verify_directory(out, strict=True)["ok"])
        self.assertTrue(manifest.verify_directory(out)["ok"])


class MalformedManifestTests(Base):
    def _write(self, data):
        out = self.d1 / "card"
        self.run_copy()
        (out / manifest.MANIFEST_NAME).write_text(json.dumps(data), encoding="utf-8")
        return out

    def test_malformed_manifests_exit_2(self):
        good = {"format": "cardsafe-manifest", "version": 1, "algorithm": "sha256"}
        cases = [
            [],
            123,
            {**good, "files": [1]},
            {**good, "files": [{"path": "a"}]},
            {**good, "files": [{"path": None, "size": 1, "hash": "x"}]},
            {**good, "files": [{"path": "a", "size": "1", "hash": "x"}]},
            {**good, "algorithm": "bogus", "files": []},
            {**good, "files": [{"path": "a", "size": 1, "hash": "x"}, {"path": "a", "size": 1, "hash": "x"}]},
        ]
        for data in cases:
            out = self._write(data)
            self.assertEqual(cli.main(["verify", str(out), "--quiet"]), 2, data)

    def test_os_error_is_clean_exit(self):
        with mock.patch("cardsafe.cli.verify_directory", side_effect=PermissionError("denied")):
            self.assertEqual(cli.main(["verify", str(self.tmp)]), 2)


class CaseCollisionTests(Base):
    def test_case_insensitive_collision_is_not_overwritten(self):
        probe = self.tmp / "probe.x"
        probe.write_text("1")
        if not (self.tmp / "PROBE.X").exists():
            self.skipTest("filesystem is case sensitive")
        (self.src / "IMG.JPG").write_bytes(b"new")
        (self.d1 / "card").mkdir(parents=True)
        (self.d1 / "card/img.jpg").write_bytes(b"old")
        for force in (False, True):
            report = copier.copy_card(self.src, [self.d1], folder="card", force=force)
            self.assertFalse(report["ok"])
            self.assertEqual((self.d1 / "card/img.jpg").read_bytes(), b"old")


class WindowsNamesTests(Base):
    def test_invalid_names_detected(self):
        for bad in ("CON", "nul.txt", "COM1", "a?b", "name.", "name "):
            self.assertIsNotNone(copier.invalid_windows_name(bad), bad)
        for good in ("IMG_0001.CR3", "CONSOLE", "a b"):
            self.assertIsNone(copier.invalid_windows_name(good), good)

    def test_folder_template_checked_on_windows(self):
        with mock.patch.object(copier, "_IS_WINDOWS", True):
            with self.assertRaises(copier.CardsafeError):
                copier.folder_parts("shoot*1", self.src)
            with self.assertRaises(copier.CardsafeError):
                copier.folder_parts("NUL", self.src)
        # a source folder named like a device is prefixed, not rejected
        aux = self.tmp / "AUX"
        aux.mkdir()
        self.assertEqual(copier.folder_parts("{name}", aux), ["_AUX"])


class ProgressTests(Base):
    def test_progress_covers_verification_and_verify_command(self):
        calls = []
        with mock.patch.object(copier, "CHUNK_SIZE", 1024), mock.patch.object(hashing, "CHUNK_SIZE", 1024):
            report = self.run_copy(progress=lambda d, t, rel: calls.append((d, t)))
            self.assertTrue(report["ok"])
            self.assertEqual(calls[-1][0], calls[-1][1])
            self.assertEqual(calls[-1][1], report["bytes"] * 3)
            self.assertEqual([c[0] for c in calls], sorted(c[0] for c in calls))
            vcalls = []
            manifest.verify_directory(self.d1 / "card", progress=lambda d, t, rel: vcalls.append((d, t)))
        self.assertEqual(vcalls[-1][0], vcalls[-1][1])
        self.assertGreater(len(vcalls), len(self.files))


class MiscTests(Base):
    def test_unknown_algorithm(self):
        with self.assertRaises(ValueError):
            hashing.new_hasher("nope")
        with self.assertRaises(ValueError):
            copier.copy_card(self.src, [self.d1], algorithm="nope")

    def test_dest_symlink_into_source_rejected(self):
        link = self.tmp / "disk1"
        try_symlink(link, self.src, is_dir=True)
        with self.assertRaises(copier.CardsafeError):
            copier.copy_card(self.src, [self.tmp], folder="disk1")

    def test_source_unchanged_by_all_of_this(self):
        before = snapshot(self.src)
        self.run_copy(force=True)
        self.assertEqual(snapshot(self.src), before)


if __name__ == "__main__":
    unittest.main()
