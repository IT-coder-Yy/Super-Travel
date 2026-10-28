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
        spec = importlib.util.spec_from_file_location("native_upgrade", Path(__file__).with_name("smoke-native-update.py"))
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

    def make_part(self,root,target,version="0.1.0"):
        _,folder,_,extension=release.TARGETS[target]
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

    def test_metadata_refuses_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            part=self.make_part(root,next(iter(release.TARGETS)))
            metadata=json.loads((part/"platform.json").read_text())
            metadata["files"][0]["name"]="../private.key"
            (part/"platform.json").write_text(json.dumps(metadata))
            with self.assertRaises(ValueError): release.merge(root/"collected",root/"release")

if __name__=="__main__":
    subprocess.run(["node","--test","scripts/configure-updater.test.mjs","scripts/configure-tools.test.mjs"],cwd=ROOT,check=True)
    subprocess.run([sys.executable,"scripts/test-windows-update.py"],cwd=ROOT,check=True)
    unittest.main()
