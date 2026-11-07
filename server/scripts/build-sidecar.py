"""Build a native, self-contained Python sidecar; never cross-freeze an interpreter."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Darwin", "arm64"): "aarch64-apple-darwin",
    ("Darwin", "x86_64"): "x86_64-apple-darwin",
}

def freeze(work: Path, assets: list[Path], output: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    resource_args = []
    for asset in assets:
        relative = (asset if asset.is_dir() else asset.parent).relative_to(ROOT / "server/src/assets")
        destination = Path("supertravel_server/assets") / relative
        resource_args.extend(("--add-data", f"{asset}{os.pathsep}{destination.as_posix()}"))
    # PyInstaller cannot follow setuptools' editable package-to-directory mapping.
    with TemporaryDirectory(prefix="source-", dir=work) as temporary:
        package = Path(temporary) / "supertravel_server"
        package.mkdir()
        for source in (ROOT / "server/src").glob("*.py"):
            shutil.copy2(source, package / source.name)
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                   "--name", "supertravel-backend", "--distpath", str(output),
                   "--workpath", str(work / "build"), "--specpath", str(work),
                   "--paths", temporary, "--collect-submodules", "uvicorn",
                   *resource_args, str(package / "main.py")]
        subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=os.environ.get("TAURI_ENV_TARGET_TRIPLE"))
    parser.add_argument("--allow-missing-pdf-assets", action="store_true", help="Development only: allow startup without PDF resources")
    args = parser.parse_args()
    native = TARGETS.get((platform.system(), platform.machine()))
    if not native or (args.target and args.target != native):
        raise SystemExit(f"PyInstaller must run natively: detected {native}, requested {args.target}. Use the matching CI runner.")
    target = args.target or native
    asset_tool = runpy.run_path(str(ROOT / "server/scripts/prepare-pdf-assets.py"))
    try:
        assets = asset_tool["prepare"](ROOT / "server/src/assets", ROOT / ".local/pdf-assets")
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        if not args.allow_missing_pdf_assets:
            raise
        print(f"PDF resources unavailable in development: {error}; run server/scripts/prepare-pdf-assets.py and restart development before retrying PDF export.", file=sys.stderr)
        assets = []
    work = ROOT / ".local" / "pyinstaller" / target
    freeze(work, assets, work / "dist")
    extension = ".exe" if platform.system() == "Windows" else ""
    source = work / "dist" / f"supertravel-backend{extension}"
    destination = ROOT / "client/src-tauri" / "binaries" / f"supertravel-backend-{target}{extension}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if os.name != "nt": destination.chmod(0o755)
    print(f"Sidecar: {destination.relative_to(ROOT)}")

if __name__ == "__main__": main()
