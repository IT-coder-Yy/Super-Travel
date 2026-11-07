"""Small release integrity tests, without touching a real installation or private key."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("prepare_release",Path(__file__).with_name("prepare-release.py"))
release=importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

def signature(version="0.1.0"):
    # This fixture only exercises publisher parsing. Cryptographic verification is performed by the native updater.
    return base64.b64encode(f"untrusted comment: fixture\nRWQfake\ntrusted comment: timestamp:1\tversion:{version}\nfixture\n".encode()).decode()

class ReleaseTests(unittest.TestCase):
    def test_native_upgrade_fixture_detects_changed_data(self):
        spec = importlib.util.spec_from_file_location("native_upgrade", ROOT / "client/scripts/smoke-native-update.py")
        upgrade = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(upgrade)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = upgrade.make_profile(root / "profile")
            upgrade.assert_profile(snapshot)
            conversation = next(path for path in snapshot if path.name != "desktop.json")
            data = json.loads(conversation.read_text(encoding="utf-8"))["data"]
            self.assertTrue(data["draft"] and data["messages"] and data["days"])
            before = upgrade.fingerprint(root)
            conversation.write_bytes(b"unexpected rewrite")
            self.assertNotEqual(upgrade.fingerprint(root), before)
            with self.assertRaises(RuntimeError):
                upgrade.assert_profile(snapshot)

    def test_old_manifest_uses_current_release_after_repository_rename(self):
        import io
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("native_upgrade", ROOT / "client/scripts/smoke-native-update.py")
        upgrade = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(upgrade)
        prefix = "https://github.com/owner/current/releases/download/v0.1.2"
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "old-package"
            for repository in ("previous", "current"):
                recorded = f"https://github.com/owner/{repository}/releases/download/v0.1.2/SuperTravel%20Windows.exe"
                canonical = upgrade.release_asset_url(recorded, prefix)
                with patch.object(upgrade, "urlopen", return_value=io.BytesIO(b"release artifact")) as opened:
                    upgrade.download(canonical, target, prefix)
                    self.assertEqual(opened.call_args.args[0].full_url, prefix + "/SuperTravel%20Windows.exe")
                    self.assertEqual(target.read_bytes(), b"release artifact")
            for name in ("", ".", "..", "%2Ftmp%2Fartifact", "..%5Cartifact"):
                with self.assertRaises(ValueError):
                    upgrade.release_asset_url("https://github.com/owner/previous/releases/download/v0.1.2/" + name, prefix)
            with patch.object(upgrade, "urlopen") as opened:
                with self.assertRaises(ValueError):
                    upgrade.download("https://example.invalid/asset.exe", target, prefix)
                opened.assert_not_called()

    def make_part(self,root,target,version="0.1.0"):
        _,folder,_,extension,_=release.TARGETS[target]
        bundle=root/target/"bundle"
        (bundle/folder).mkdir(parents=True)
        asset=bundle/folder/("app"+extension)
        asset.write_bytes(b"signed artifact fixture")
        Path(str(asset)+".sig").write_text(signature(version))
        if "apple-darwin" in target:
            (bundle/"dmg").mkdir()
            (bundle/"dmg"/"app.dmg").write_bytes(b"installer fixture")
        out=root/"collected"/target
        release.collect(bundle,target,version,"https://github.com/example/app/releases/download/v"+version,out)
        return out

    def test_signature_version_and_missing_signature(self):
        release.signature_text(signature(),"0.1.0")
        with self.assertRaises(ValueError): release.signature_text(signature(),"0.1.1")
        with self.assertRaises(ValueError): release.signature_text("not a signature","0.1.0")

    def test_all_platforms_are_required_and_checksums_detect_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            first=self.make_part(root,next(iter(release.TARGETS)))
            with self.assertRaises(ValueError): release.merge(root/"collected",root/"release")
            for target in list(release.TARGETS)[1:]: self.make_part(root,target)
            manifest=release.merge(root/"collected",root/"release")
            self.assertEqual(len(manifest["platforms"]),4)
            metadata=json.loads((first/"platform.json").read_text())
            (first/metadata["files"][0]["name"]).write_bytes(b"tampered")
            with self.assertRaises(ValueError): release.merge(root/"collected",root/"bad-release")

    def test_release_names_match_update_urls_signatures_and_checksums(self):
        expected = {
            "x86_64-pc-windows-msvc": ("windows-x86_64-nsis", "SuperTravel_0.1.4_x64-setup.exe", None),
            "aarch64-apple-darwin": ("darwin-aarch64-app", "SuperTravel_0.1.4_aarch64-apple-darwin.app.tar.gz", "SuperTravel_0.1.4_aarch64.dmg"),
            "x86_64-apple-darwin": ("darwin-x86_64-app", "SuperTravel_0.1.4_x86_64-apple-darwin.app.tar.gz", "SuperTravel_0.1.4_x64.dmg"),
            "x86_64-unknown-linux-gnu": ("linux-x86_64-appimage", "SuperTravel_0.1.4_amd64.AppImage", None),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filenames = set()
            expected_platforms = {}
            for target, (platform, name, dmg) in expected.items():
                with self.subTest(target=target):
                    part = self.make_part(root, target, "0.1.4")
                    metadata = json.loads((part / "platform.json").read_text(encoding="utf-8"))
                    names = {name, name + ".sig"} | ({dmg} if dmg else set())
                    self.assertEqual(metadata["platform"], platform)
                    self.assertEqual({item["name"] for item in metadata["files"]}, names)
                    self.assertEqual((part / name).read_bytes(), b"signed artifact fixture")
                    self.assertEqual((part / (name + ".sig")).read_text(encoding="utf-8"), signature("0.1.4") + "\n")
                    if dmg:
                        self.assertEqual((part / dmg).read_bytes(), b"installer fixture")
                    filenames.update(names)
                    expected_platforms[platform] = {
                        "signature": signature("0.1.4"),
                        "url": "https://github.com/example/app/releases/download/v0.1.4/" + name,
                    }
            output = root / "release"
            release.merge(root / "collected", output)
            manifest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["platforms"], expected_platforms)
            self.assertEqual({path.name for path in output.iterdir()}, filenames | {"latest.json", "SHA256SUMS.txt"})
            checksums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(checksums, sorted(f"{release.digest(output / name)}  {name}" for name in filenames))

    def test_metadata_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            part=self.make_part(root,next(iter(release.TARGETS)))
            metadata=json.loads((part/"platform.json").read_text())
            metadata["files"][0]["name"]="../private.key"
            (part/"platform.json").write_text(json.dumps(metadata))
            with self.assertRaises(ValueError): release.merge(root/"collected",root/"release")

    def test_merge_cli_uses_the_same_notes_as_the_github_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in release.TARGETS:
                self.make_part(root, target)
            subprocess.run([sys.executable, str(ROOT / "scripts/prepare-release.py"),
                            "--merge", str(root / "collected"), "--output", str(root / "release")],
                           cwd=ROOT, check=True, capture_output=True, text=True)
            manifest = json.loads((root / "release/latest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["notes"], (ROOT / "docs/RELEASE_NOTES.md").read_text(encoding="utf-8").strip())

if __name__=="__main__":
    subprocess.run(["node","--test","client/scripts/configure-updater.test.mjs","client/scripts/configure-tools.test.mjs","server/scripts/tools-dev.test.mjs"],cwd=ROOT,check=True)
    subprocess.run([sys.executable,"client/scripts/test-windows-update.py"],cwd=ROOT,check=True)
    unittest.main()
