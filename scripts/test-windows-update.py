"""Guard/evidence tests only; actual OS installation is exercised on the Windows runner."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("windows_update", Path(__file__).with_name("smoke-windows-update.py"))
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class WindowsUpdateTests(unittest.TestCase):
    def test_installer_refuses_local_and_self_hosted_machines(self):
        with patch.dict(update.os.environ, {}, clear=True), patch.object(update.sys, "platform", "win32"):
            with self.assertRaises(ValueError):
                update.require_hosted_windows()
        with patch.dict(update.os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}, clear=True), patch.object(update.sys, "platform", "win32"):
            with self.assertRaises(ValueError):
                update.require_hosted_windows()

    def test_installer_requires_existing_hosted_temp_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(update.os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_TEMP": temporary}, clear=True), patch.object(update.sys, "platform", "win32"):
                self.assertEqual(update.require_hosted_windows(), Path(temporary).resolve())

    def test_exit_zero_is_not_evidence_without_exact_marker(self):
        for mode in ("tampered", "interrupted", "success"):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                update.require_probe_result(subprocess.CompletedProcess([], 0, ""), mode)
        result = subprocess.CompletedProcess([], 0, "SUPERTRAVEL_UPDATE_PROBE_OK:tampered\n")
        update.require_probe_result(result, "tampered")
        with self.assertRaises(RuntimeError):
            update.require_probe_result(result, "interrupted")

    def test_windows_install_requires_verified_download_before_separate_file_checks(self):
        marker = "SUPERTRAVEL_UPDATE_PROBE_VERIFIED:success\n"
        update.require_probe_result(subprocess.CompletedProcess([], 0, marker), "success")
        with self.assertRaises(subprocess.CalledProcessError):
            update.require_probe_result(subprocess.CompletedProcess([], 1, marker), "success")


if __name__ == "__main__":
    unittest.main()
