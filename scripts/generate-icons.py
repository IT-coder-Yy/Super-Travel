"""Generate desktop icons from the approved SVG using the installed Tauri CLI."""
from pathlib import Path
from shutil import copyfile
from subprocess import run
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def main():
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    icons = ROOT / "src-tauri" / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="icons-", dir=local) as output:
        run(["node", str(ROOT / "node_modules/@tauri-apps/cli/tauri.js"),
             "icon", str(ROOT / "src/assets/app-icon.svg"), "--output", output],
            cwd=ROOT, check=True)
        for name in ("32x32.png", "128x128.png", "icon.ico", "icon.icns"):
            copyfile(Path(output) / name, icons / name)


if __name__ == "__main__":
    main()
