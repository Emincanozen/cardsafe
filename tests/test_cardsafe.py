import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from cardsafe import cli, copier, manifest
from cardsafe.hashing import hash_file


def make_card(root: Path) -> dict:
    files = {
        "DCIM/100CANON/IMG_0001.CR3": b"A" * 5000,
        "DCIM/100CANON/IMG_0002.CR3": bytes(range(256)) * 40,
        "PRIVATE/CLIP.MP4": b"video" * 3000,
        "EMPTY.TXT": b"",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return files


def snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cardsafe-test-"))
        self.src = self.tmp / "EOS_R5"
        self.src.mkdir()
        self.files = make_card(self.src)
        self.d1 = self.tmp / "disk1"
        self.d2 = self.tmp / "disk2"
        copier._after_write_hook = None

    def tearDown(self):
        copier._after_write_hook = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_copy(self, **kw):
        kw.setdefault("folder", "card")
        return copier.copy_card(self.src, [self.d1, self.d2], **kw)


class CopyTests(Base):
    def test_multi_destination_copy_and_manifest(self):
        for algo in ("sha256", "blake2b"):
            shutil.rmtree(self.d1, ignore_errors=True)
            shutil.rmtree(self.d2, ignore_errors=True)
            report = self.run_copy(algorithm=algo)
            self.assertTrue(report["ok"], report)
            for d in (self.d1, self.d2):
                out = d / "card"
                got = snapshot(out)
                got.pop(manifest.MANIFEST_NAME)
                got.pop(manifest.REPORT_NAME)
                self.assertEqual(got, self.files)
                m = json.loads((out / manifest.MANIFEST_NAME).read_text(encoding="utf-8"))
                self.assertEqual(m["format"], "cardsafe-manifest")
                self.assertEqual(m["algorithm"], algo)
                self.assertEqual(len(m["files"]), len(self.files))
                for e in m["files"]:
                    self.assertEqual(e["hash"], hash_file(out / e["path"], algo))
                    self.assertEqual(e["size"], len(self.files[e["path"]]))
                self.assertFalse(list(out.rglob("*" + manifest.PART_SUFFIX)))

    def test_source_untouched(self):
        before = snapshot(self.src)
        self.run_copy()
        self.assertEqual(snapshot(self.src), before)

    def test_folder_template(self):
        report = copier.copy_card(self.src, [self.d1], folder="{name}/{date}")
        self.assertTrue(report["ok"])
        self.assertTrue((self.d1 / "EOS_R5").is_dir())

    def test_unsafe_template_rejected(self):
        with self.assertRaises(copier.CardsafeError):
            copier.copy_card(self.src, [self.d1], folder="../x")

    def test_dest_inside_source_rejected(self):
        with self.assertRaises(copier.CardsafeError):
            copier.copy_card(self.src, [self.src / "out"], folder="card")

    def test_dry_run_writes_nothing(self):
        report = self.run_copy(dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertFalse(self.d1.exists() or self.d2.exists())
        self.assertEqual(report["counts"]["would-copy"], 2 * len(self.files))

    def test_missing_source(self):
        with self.assertRaises(copier.CardsafeError):
            copier.copy_card(self.tmp / "nope", [self.d1])


class CorruptionTests(Base):
    def test_corrupt_destination_detected(self):
        def corrupt(part: Path):
            if "disk2" in part.parts and part.name.startswith("IMG_0001"):
                data = bytearray(part.read_bytes())
                data[10] ^= 0xFF
                part.write_bytes(bytes(data))

        copier._after_write_hook = corrupt
        report = self.run_copy()
        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertFalse((self.d2 / "card/DCIM/100CANON/IMG_0001.CR3").exists())
        self.assertTrue((self.d1 / "card/DCIM/100CANON/IMG_0001.CR3").exists())
        m = json.loads((self.d2 / "card" / manifest.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertNotIn("DCIM/100CANON/IMG_0001.CR3", [e["path"] for e in m["files"]])
        # the bad file is simply recopied on resume
        copier._after_write_hook = None
        report = self.run_copy()
        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["copied"], 1)

    def test_verify_detects_later_corruption_and_missing(self):
        self.run_copy()
        out = self.d1 / "card"
        self.assertTrue(manifest.verify_directory(out)["ok"])
        victim = out / "PRIVATE/CLIP.MP4"
        data = bytearray(victim.read_bytes())
        data[0] ^= 1
        victim.write_bytes(bytes(data))
        (out / "EMPTY.TXT").unlink()
        rep = manifest.verify_directory(out)
        self.assertFalse(rep["ok"])
        status = {r["path"]: r["status"] for r in rep["results"]}
        self.assertEqual(status["PRIVATE/CLIP.MP4"], "hash-mismatch")
        self.assertEqual(status["EMPTY.TXT"], "missing")

    def test_verify_strict_extra_files(self):
        self.run_copy()
        out = self.d1 / "card"
        (out / "stray.txt").write_text("x")
        self.assertTrue(manifest.verify_directory(out)["ok"])
        rep = manifest.verify_directory(out, strict=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["extra_files"], ["stray.txt"])

    def test_manifest_path_traversal_rejected(self):
        self.run_copy()
        out = self.d1 / "card"
        m = json.loads((out / manifest.MANIFEST_NAME).read_text(encoding="utf-8"))
        m["files"][0]["path"] = "../disk2/card/EMPTY.TXT"
        (out / manifest.MANIFEST_NAME).write_text(json.dumps(m), encoding="utf-8")
        rep = manifest.verify_directory(out)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["results"][0]["status"], "invalid-path")


class ResumeAndCollisionTests(Base):
    def test_resume_skips_identical_and_finishes_rest(self):
        self.run_copy()
        victim = self.d1 / "card/PRIVATE/CLIP.MP4"
        victim.unlink()
        # leftover partial file from an interrupted run
        (self.d1 / "card/DCIM/100CANON/IMG_0002.CR3.cardsafe-part").write_bytes(b"junk")
        report = self.run_copy()
        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["copied"], 1)
        self.assertEqual(report["counts"]["skipped"], 2 * len(self.files) - 1)
        self.assertEqual(victim.read_bytes(), self.files["PRIVATE/CLIP.MP4"])
        self.assertTrue(manifest.verify_directory(self.d1 / "card")["ok"])

    def test_collision_not_overwritten_without_force(self):
        self.run_copy()
        target = self.d1 / "card/DCIM/100CANON/IMG_0001.CR3"
        # same size, different content -> hash conflict
        target.write_bytes(b"B" * 5000)
        # different size -> size conflict
        other = self.d1 / "card/PRIVATE/CLIP.MP4"
        other.write_bytes(b"short")
        report = self.run_copy()
        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"]["conflict"], 2)
        self.assertEqual(target.read_bytes(), b"B" * 5000)
        self.assertEqual(other.read_bytes(), b"short")

    def test_force_overwrites(self):
        self.run_copy()
        target = self.d1 / "card/DCIM/100CANON/IMG_0001.CR3"
        target.write_bytes(b"B" * 5000)
        (self.d1 / "card/PRIVATE/CLIP.MP4").write_bytes(b"short")
        report = self.run_copy(force=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["counts"]["overwritten"], 2)
        self.assertEqual(target.read_bytes(), self.files["DCIM/100CANON/IMG_0001.CR3"])
        self.assertTrue(manifest.verify_directory(self.d1 / "card")["ok"])


class CliTests(Base):
    def call(self, *argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_copy_json_and_verify_exit_codes(self):
        code, out = self.call("copy", str(self.src), str(self.d1), str(self.d2), "--folder", "card", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["counts"]["copied"], 2 * len(self.files))
        code, _ = self.call("verify", str(self.d2 / "card"), "--quiet")
        self.assertEqual(code, 0)
        victim = self.d2 / "card/PRIVATE/CLIP.MP4"
        victim.write_bytes(b"corrupted!" * 100)
        code, out = self.call("verify", str(self.d2 / "card"), "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_copy_failure_exit_code(self):
        self.call("copy", str(self.src), str(self.d1), "--folder", "card", "--quiet")
        (self.d1 / "card/EMPTY.TXT").write_bytes(b"not empty")
        code, _ = self.call("copy", str(self.src), str(self.d1), "--folder", "card", "--quiet")
        self.assertEqual(code, 1)

    def test_usage_errors(self):
        code, _ = self.call("copy", str(self.tmp / "missing"), str(self.d1))
        self.assertEqual(code, 2)
        code, _ = self.call("verify", str(self.tmp))
        self.assertEqual(code, 2)

    def test_dry_run_cli(self):
        code, out = self.call("copy", str(self.src), str(self.d1), "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(self.d1.exists())
        self.assertTrue(json.loads(out)["dry_run"])


if __name__ == "__main__":
    unittest.main()
