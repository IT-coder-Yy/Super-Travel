"""Collect signed native update artifacts and merge a complete four-architecture manifest."""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "x86_64-pc-windows-msvc": ("windows-x86_64-nsis", "nsis", "*.exe", ".exe"),
    "aarch64-apple-darwin": ("darwin-aarch64-app", "macos", "*.app.tar.gz", ".app.tar.gz"),
    "x86_64-apple-darwin": ("darwin-x86_64-app", "macos", "*.app.tar.gz", ".app.tar.gz"),
    "x86_64-unknown-linux-gnu": ("linux-x86_64-appimage", "appimage", "*.AppImage", ".AppImage"),
}
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")

def digest(path: Path) -> str:
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()

def signature_text(value: str, version: str) -> str:
    try: decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as error: raise ValueError("Invalid Tauri signature encoding") from error
    lines = decoded.splitlines()
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment:"):
        raise ValueError("Invalid minisign signature structure")
    # A signature bound to the app version prevents an inflated manifest from replaying an old valid package.
    if not re.search(r"(?:^|[\s;])(?:app[_-]?)?version[:=]\s*" + re.escape(version) + r"(?:\s|;|$)", lines[2]):
        raise ValueError(f"Signature is not bound to app version {version}; use a current Tauri CLI or signer sign --app-version")
    return value

def safe_base_url(value: str) -> str:
    parsed = urlparse(value)
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Asset base URL must be HTTPS (localhost HTTP is allowed for explicit local rehearsal)")
    return value.rstrip("/")

def collect(bundle: Path, target: str, version: str, base_url: str, output: Path) -> dict:
    if not SEMVER.fullmatch(version): raise ValueError("Release requires stable SemVer")
    key, folder, pattern, extension = TARGETS[target]
    base_url = safe_base_url(base_url)
    matches = list((bundle / folder).glob(pattern))
    if len(matches) != 1: raise ValueError(f"Expected one {target} update artifact, found {len(matches)}")
    asset = matches[0]
    signature_file = Path(str(asset) + ".sig")
    signature = signature_text(signature_file.read_text(encoding="utf-8").strip(), version)
    output.mkdir(parents=True, exist_ok=True)
    name = f"SuperTravel_{version}_{key}{extension}"
    shutil.copy2(asset, output / name)
    (output / f"{name}.sig").write_text(signature + "\n", encoding="utf-8")
    files = [{"name": name, "sha256": digest(output / name)}, {"name": f"{name}.sig", "sha256": digest(output / f"{name}.sig")}]
    if "apple-darwin" in target:
        dmgs = list((bundle / "dmg").glob("*.dmg"))
        if len(dmgs) != 1: raise ValueError(f"Expected one macOS DMG, found {len(dmgs)}")
        dmg_name = f"SuperTravel_{version}_{key}.dmg"
        shutil.copy2(dmgs[0], output / dmg_name)
        files.append({"name": dmg_name, "sha256": digest(output / dmg_name)})
    manifest = {"version": version, "platform": key, "url": f"{base_url}/{quote(name)}", "signature": signature, "files": files}
    (output / "platform.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest

def merge(source: Path, output: Path, notes: str = "桌面骨架验证版本；旅行模型与真实地图服务尚未接入。") -> dict:
    parts = list(source.rglob("platform.json"))
    expected = {target[0] for target in TARGETS.values()}
    found: dict[str, dict] = {}
    versions: set[str] = set()
    for part in parts:
        entry = json.loads(part.read_text(encoding="utf-8"))
        key = entry["platform"]
        if key not in expected or key in found: raise ValueError(f"Unknown or duplicate platform: {key}")
        if not SEMVER.fullmatch(entry["version"]): raise ValueError("Invalid release version")
        signature_text(entry["signature"], entry["version"])
        safe_base_url(entry["url"])
        for item in entry["files"]:
            if Path(item["name"]).name != item["name"] or "/" in item["name"] or "\\" in item["name"]: raise ValueError("Unsafe artifact filename")
            if digest(part.parent / item["name"]) != item["sha256"]: raise ValueError(f"Artifact changed: {item['name']}")
        found[key] = entry
        versions.add(entry["version"])
    if set(found) != expected or len(versions) != 1: raise ValueError(f"All four matching platforms are required; found {sorted(found)}, versions={sorted(versions)}")
    output.mkdir(parents=True, exist_ok=True)
    checksums = []
    for part in parts:
        entry = json.loads(part.read_text(encoding="utf-8"))
        for item in entry["files"]:
            shutil.copy2(part.parent / item["name"], output / item["name"])
            checksums.append(f"{item['sha256']}  {item['name']}")
    manifest = {"version": versions.pop(), "notes": notes, "pub_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "platforms": {key: {"signature": item["signature"], "url": item["url"]} for key,item in sorted(found.items())}}
    (output / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text("\n".join(sorted(checksums)) + "\n", encoding="utf-8")
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--version", default=json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"])
    parser.add_argument("--merge", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.merge:
        notes = (ROOT / "docs/RELEASE_NOTES.md").read_text(encoding="utf-8").strip()
        result = merge(args.merge, args.output, notes=notes)
    elif args.target and args.bundle_dir and args.base_url: result = collect(args.bundle_dir, args.target, args.version, args.base_url, args.output)
    else: parser.error("Use --merge, or --target + --bundle-dir + --base-url")
    print(f"Prepared {result.get('platform','all platforms')} version {result['version']} in {args.output}")

if __name__ == "__main__": main()
