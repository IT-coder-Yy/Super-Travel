"""Asset downloads must be verified, recoverable, and unnecessary on a warm cache."""
from contextlib import ExitStack
from io import BytesIO
import hashlib
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

spec = importlib.util.spec_from_file_location("pdf_assets", Path(__file__).with_name("prepare-pdf-assets.py"))
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)
sha = lambda data: hashlib.sha256(data).hexdigest()


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def test_download_reuses_only_verified_cache(self):
        expected = b"verified archive"
        target = self.cache / "sample"
        target.write_bytes(expected)
        with patch.dict(assets.SOURCES, {"sample": ("https://example.invalid/sample", sha(expected))}), patch.object(assets, "urlopen") as network:
            self.assertEqual(assets.download("sample", self.cache), target)
            network.assert_not_called()
            target.write_bytes(b"damaged archive")
            network.return_value = BytesIO(expected)
            self.assertEqual(assets.download("sample", self.cache).read_bytes(), expected)
            self.assertEqual(network.call_count, 1)

    def test_retry_can_recover_without_publishing_partial_download(self):
        with patch.dict(assets.SOURCES, {"sample": ("https://example.invalid/sample", sha(b"complete"))}), patch.object(assets, "urlopen", side_effect=[URLError("offline"), BytesIO(b"complete")]) as network:
            self.assertEqual(assets.download("sample", self.cache).read_bytes(), b"complete")
            self.assertEqual(network.call_count, 2)
            self.assertEqual([p.name for p in self.cache.iterdir()], ["sample"])

    def test_bad_checksum_size_and_network_leave_existing_file_unchanged(self):
        target = self.cache / "sample"
        for failure in ("checksum", "size", "network"):
            with self.subTest(failure=failure), ExitStack() as stack:
                target.write_bytes(b"previous")
                stack.enter_context(patch.dict(assets.SOURCES, {"sample": ("https://example.invalid/sample", sha(b"valid"))}))
                if failure == "network":
                    network = stack.enter_context(patch.object(assets, "urlopen", side_effect=URLError("offline")))
                else:
                    network = stack.enter_context(patch.object(assets, "urlopen", side_effect=lambda *a, **k: BytesIO(b"wrong")))
                if failure == "size":
                    stack.enter_context(patch.object(assets, "MAX_DOWNLOAD", 2))
                with self.assertRaises(RuntimeError):
                    assets.download("sample", self.cache)
                self.assertEqual(network.call_count, 2)
                self.assertEqual(target.read_bytes(), b"previous")
                self.assertEqual([p.name for p in self.cache.iterdir()], ["sample"])

    def test_bad_generated_output_does_not_replace_existing_font(self):
        target = self.root / "font.ttf"
        target.write_bytes(b"original")
        with self.assertRaises(ValueError):
            assets.write_checked(target, b"bad", sha(b"good"))
        self.assertEqual(target.read_bytes(), b"original")
        assets.write_checked(target, b"good", sha(b"good"))
        self.assertEqual(target.read_bytes(), b"good")

    def emoji_archive(self, name="1f600.png", kind=tarfile.REGTYPE):
        data = b"fixed image bytes"
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo(f"twemoji-{assets.TWEMOJI_REV}/assets/72x72/{name}")
            entry.type = kind
            entry.size = len(data) if kind == tarfile.REGTYPE else 0
            entry.linkname = "../outside"
            archive.addfile(entry, BytesIO(data) if entry.size else None)
        raw = buffer.getvalue()
        (self.cache / "twemoji.tar.gz").write_bytes(raw)
        return raw, data

    def test_missing_and_corrupt_emoji_are_repaired_from_verified_archive(self):
        raw, data = self.emoji_archive()
        expected = sha(b"1f600.png\0" + hashlib.sha256(data).digest())
        output = self.root / "assets"
        with patch.dict(assets.SOURCES, {"twemoji.tar.gz": ("https://example.invalid/archive", sha(raw))}), patch.object(assets, "EMOJI_TEXT", {}), patch.object(assets, "EMOJI_HASH", expected), patch.object(assets, "urlopen") as network:
            assets.prepare_emoji(output, self.cache)
            target = output / "emoji/72x72/1f600.png"
            self.assertEqual(target.read_bytes(), data)
            target.write_bytes(b"damaged")
            assets.prepare_emoji(output, self.cache)
            self.assertEqual(target.read_bytes(), data)
            assets.prepare_emoji(output, self.cache)
            network.assert_not_called()

    def test_archive_paths_and_links_cannot_escape_resource_directory(self):
        for name, kind in (("../../../outside", tarfile.REGTYPE), ("1f600.png", tarfile.SYMTYPE)):
            with self.subTest(name=name):
                raw, _ = self.emoji_archive(name, kind)
                with patch.dict(assets.SOURCES, {"twemoji.tar.gz": ("https://example.invalid/archive", sha(raw))}), patch.object(assets, "EMOJI_TEXT", {}):
                    with self.assertRaisesRegex(ValueError, "Unexpected emoji archive entry"):
                        assets.prepare_emoji(self.root / "assets", self.cache)
                self.assertFalse((self.root / "outside").exists())

    def test_packaging_requires_assets_but_development_can_continue(self):
        spec = importlib.util.spec_from_file_location("sidecar", Path(__file__).with_name("build-sidecar.py"))
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        def unavailable(*args):
            raise RuntimeError("offline")
        with patch.object(builder, "ROOT", self.root), patch.object(builder.platform, "system", return_value="Windows"), patch.object(builder.platform, "machine", return_value="AMD64"), patch.object(builder.runpy, "run_path", return_value={"prepare": unavailable}), patch.object(builder.subprocess, "run") as freeze, patch.object(builder.shutil, "copy2"):
            with patch("sys.argv", ["build-sidecar.py"]), self.assertRaisesRegex(RuntimeError, "offline"):
                builder.main()
            freeze.assert_not_called()
            with patch("sys.argv", ["build-sidecar.py", "--allow-missing-pdf-assets"]):
                builder.main()
            self.assertNotIn("--add-data", freeze.call_args.args[0])

    def test_ready_assets_work_offline_and_bundle_excludes_local_reports(self):
        output = self.root / "assets"
        (output / "fonts").mkdir(parents=True)
        (output / "fonts/NotoSansSC-Regular.ttf").write_bytes(b"font")
        (output / "fonts/OFL.txt").write_bytes(b"license")
        (output / "fonts/fallback-manifest.json").write_text("local report")
        with patch.object(assets, "FONT_HASHES", {"NotoSansSC-Regular.ttf": sha(b"font")}), patch.object(assets, "JIGMO_TEXT", {}), patch.object(assets, "EMOJI_TEXT", {}), patch.object(assets, "EMOJI_HASH", sha(b"")), patch.dict(assets.SOURCES, {"OFL.txt": ("https://example.invalid/license", sha(b"license"))}), patch.object(assets, "urlopen", side_effect=AssertionError("offline")):
            paths = assets.prepare(output, self.cache)
            self.assertTrue((output / "NOTICE.txt").is_file())
            self.assertIn(output / "fonts/OFL.txt", paths)
            self.assertNotIn(output / "fonts/fallback-manifest.json", paths)
            self.assertEqual(assets.prepare(output, self.cache), paths)


if __name__ == "__main__":
    unittest.main()
