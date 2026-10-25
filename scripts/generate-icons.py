"""Render the shared SVG mark using the already installed Tauri CLI."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    generated = ROOT / ".local" / "generated-icons"
    subprocess.run([
        "node", str(ROOT / "node_modules/@tauri-apps/cli/tauri.js"), "icon",
        str(ROOT / "public/app-icon.svg"), "--output", str(generated),
    ], cwd=ROOT, check=True)
    destination = ROOT / "src-tauri/icons"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("32x32.png", "128x128.png", "icon.ico", "icon.icns"):
        shutil.copyfile(generated / name, destination / name)


if __name__ == "__main__":
    main()
