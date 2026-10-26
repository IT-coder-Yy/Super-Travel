"""Build a native, self-contained Python sidecar; never cross-freeze an interpreter."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
}

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=os.environ.get("TAURI_ENV_TARGET_TRIPLE"))
    args = parser.parse_args()
    native = TARGETS.get((platform.system(), platform.machine()))
    if not native or (args.target and args.target != native):
        raise SystemExit(f"PyInstaller must run natively: detected {native}, requested {args.target}. Use the matching CI runner.")
    target = args.target or native
    work = ROOT / ".local" / "pyinstaller" / target
    work.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
               "--name", "supertravel-backend", "--distpath", str(work / "dist"),
               "--workpath", str(work / "build"), "--specpath", str(work),
               "--paths", str(ROOT), "--collect-submodules", "uvicorn", str(ROOT / "backend" / "main.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    extension = ".exe" if platform.system() == "Windows" else ""
    source = work / "dist" / f"supertravel-backend{extension}"
    destination = ROOT / "src-tauri" / "binaries" / f"supertravel-backend-{target}{extension}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if os.name != "nt": destination.chmod(0o755)
    print(f"Sidecar: {destination.relative_to(ROOT)}")

if __name__ == "__main__": main()
