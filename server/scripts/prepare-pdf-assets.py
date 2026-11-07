"""Prepare pinned PDF assets locally; downloaded/generated files stay out of Git."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FONTTOOLS_VERSION = "4.68.0"
GOOGLE_REV = "6c1dbe27dcf7d817d4b62cec033ba77fa8e2a5ec"
TWEMOJI_REV = "b6b55fef1e8636b540a6d016a4729ca8cdf2e60b"
GOOGLE_URL = f"https://raw.githubusercontent.com/google/fonts/{GOOGLE_REV}/ofl/notosanssc"
SOURCES = {
    "noto.ttf": (f"{GOOGLE_URL}/NotoSansSC%5Bwght%5D.ttf", "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da"),
    "OFL.txt": (f"{GOOGLE_URL}/OFL.txt", "1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9"),
    "jigmo.zip": ("https://kamichikoichi.github.io/jigmo/Jigmo-20250912.zip", "5744c7386d129475d87607ca66d043c8793c65448adeaedc921b6931890e5d0b"),
    "twemoji.tar.gz": (f"https://codeload.github.com/jdecked/twemoji/tar.gz/{TWEMOJI_REV}", "705d79de1460e5e775f362f0d0f01fbe3ef8d65bf4648c490e4649704584f747"),
}
FONT_HASHES = {
    "NotoSansSC-Regular.ttf": "26305be85f5378dc7d53f3dfa6c852a7e46621b226423400c1821aca9fc01c41",
    "Jigmo-Fallback.ttf": "c496bcbbfab1309d9bb39bd8934c83b44c90dd01ddda1b1e0722d46d312948e6",
    "Jigmo2-Fallback.ttf": "df7c16240dc615562a6c41fd575ecb06f2fb114c6d98e06afe1225103ca146d2",
    "Jigmo3-Fallback.ttf": "d0dd539e95c265f59b77fcafcbe5692fe8f10d81ce12a249472b5c74cb1be339",
}
JIGMO_TEXT = {
    "LICENSE.txt": ("Jigmo-LICENSE.txt", "437ca31a127c9f1b191ff9a42bb563e5f7e414ae819006dca4635d9fb34aeb94"),
    "README.txt": ("Jigmo-SOURCE-README.txt", "9e21785d64d317da46f270c33f464be36ef20d1bfa30f07f5de27a1d1567162f"),
    "THANKS.txt": ("Jigmo-THANKS.txt", "e50087723cb3b8fbfaca220398b15014ec0073104449b97fc1f60485331921f6"),
}
EMOJI_TEXT = {
    "LICENSE": ("LICENSE-CODE.txt", "5076b7ec0f98f95aa87a3103bebdcd3852acd88d61fbd99307b8d953024acb67"),
    "LICENSE-GRAPHICS": ("LICENSE-GRAPHICS.txt", "8ae9438818c26e4873b91d8c6ad620526c011e27e125677f13031eda903f007c"),
}
EMOJI_HASH = "cce1d6345b5e57274210303487f07e28a8ff2614116d2c2f73300b7ab65a8ce1"
CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF), (0x2A700, 0x2B73F), (0x2B740, 0x2B81F),
    (0x2B820, 0x2CEAF), (0x2CEB0, 0x2EBEF), (0x2EBF0, 0x2EE5F),
    (0x2F800, 0x2FA1F), (0x30000, 0x3134F), (0x31350, 0x323AF), (0x323B0, 0x3347F))
NOTICE = f"""PDF resources (prepared locally; no network access during installed PDF export)
Noto Sans SC: Google Fonts revision {GOOGLE_REV}; SIL OFL 1.1, fonts/OFL.txt.
The variable font is instantiated at weight 400 with fonttools {FONTTOOLS_VERSION}.
Jigmo 2025-09-12: https://kamichikoichi.github.io/jigmo/ ; CC0 1.0, fonts/Jigmo-LICENSE.txt.
Subsets retain standard CJK characters missing from Noto; IVD and private-use glyphs are excluded.
Twemoji 17.0.3: Twitter and contributors, including Jason Sofonia and Justine De Caires.
https://github.com/jdecked/twemoji/tree/{TWEMOJI_REV}
Unmodified 72x72 PNG graphics, scaled only for layout; CC BY 4.0, https://creativecommons.org/licenses/by/4.0/ .
See emoji/LICENSE-GRAPHICS.txt; the upstream code license is emoji/LICENSE-CODE.txt.
"""
MAX_DOWNLOAD = 100 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matches(path: Path, expected: str) -> bool:
    return path.is_file() and digest(path) == expected


def write_checked(path: Path, data: bytes, expected: str) -> None:
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Resource checksum mismatch: {path.name}")
    if matches(path, expected):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        pending = Path(temporary.name)
        temporary.write(data)
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def download(name: str, cache: Path) -> Path:
    url, expected = SOURCES[name]
    target = cache / name
    if matches(target, expected):
        return target
    cache.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        pending = None
        try:
            print(f"PDF assets: downloading {name} ({attempt + 1}/2)", flush=True)
            with urlopen(url, timeout=30) as response, tempfile.NamedTemporaryFile(dir=cache, delete=False) as temporary:
                pending = Path(temporary.name)
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise ValueError(f"Resource download too large: {name}")
                    temporary.write(chunk)
            if digest(pending) != expected:
                raise ValueError(f"Resource download checksum mismatch: {name}")
            os.replace(pending, target)
            return target
        except (OSError, ValueError) as error:
            if attempt == 1:
                raise RuntimeError(f"Could not prepare {name}: {error}") from error
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    raise AssertionError("unreachable")


def emoji_digest(directory: Path) -> str:
    result = hashlib.sha256()
    for path in sorted(directory.glob("*.png")):
        result.update(path.name.encode("ascii") + b"\0" + bytes.fromhex(digest(path)))
    return result.hexdigest()


def prepare_fonts(output: Path, cache: Path) -> None:
    fonts = output / "fonts"
    if any(not matches(fonts / name, sha) for name, sha in FONT_HASHES.items()):
        import fontTools
        from fontTools import subset
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        if fontTools.__version__ != FONTTOOLS_VERSION:
            raise RuntimeError(f"Run uv sync --project server --locked (fonttools=={FONTTOOLS_VERSION}) before preparing fonts")
        with tempfile.TemporaryDirectory(dir=cache) as directory:
            temporary = Path(directory)
            if not matches(fonts / "NotoSansSC-Regular.ttf", FONT_HASHES["NotoSansSC-Regular.ttf"]):
                with TTFont(download("noto.ttf", cache), recalcTimestamp=False) as font:
                    instantiateVariableFont(font, {"wght": 400}, inplace=True, updateFontNames=True)
                    target = temporary / "noto.ttf"
                    font.save(target)
                write_checked(fonts / "NotoSansSC-Regular.ttf", target.read_bytes(), FONT_HASHES["NotoSansSC-Regular.ttf"])
            with TTFont(fonts / "NotoSansSC-Regular.ttf") as font:
                base = set(font.getBestCmap())
            if any(not matches(fonts / name, sha) for name, sha in FONT_HASHES.items() if name.startswith("Jigmo")):
                with zipfile.ZipFile(download("jigmo.zip", cache)) as archive:
                    for stem in ("Jigmo", "Jigmo2", "Jigmo3"):
                        name = f"{stem}-Fallback.ttf"
                        if matches(fonts / name, FONT_HASHES[name]):
                            continue
                        source = temporary / f"{stem}.ttf"
                        source.write_bytes(archive.read(f"{stem}.ttf"))
                        with TTFont(source, recalcTimestamp=False) as font:
                            wanted = {c for c in font.getBestCmap() if c not in base and any(a <= c <= b for a, b in CJK_RANGES)}
                            font["cmap"].tables = [table for table in font["cmap"].tables if table.format != 14]
                            options = subset.Options()
                            options.layout_features, options.hinting, options.recalc_timestamp = [], False, False
                            worker = subset.Subsetter(options=options)
                            worker.populate(unicodes=wanted)
                            worker.subset(font)
                            target = temporary / name
                            font.save(target)
                        write_checked(fonts / name, target.read_bytes(), FONT_HASHES[name])
    ofl_sha = SOURCES["OFL.txt"][1]
    if not matches(fonts / "OFL.txt", ofl_sha):
        write_checked(fonts / "OFL.txt", download("OFL.txt", cache).read_bytes(), ofl_sha)
    if any(not matches(fonts / name, sha) for name, sha in JIGMO_TEXT.values()):
        with zipfile.ZipFile(download("jigmo.zip", cache)) as archive:
            for source, (name, sha) in JIGMO_TEXT.items():
                write_checked(fonts / name, archive.read(source), sha)


def prepare_emoji(output: Path, cache: Path) -> None:
    emoji = output / "emoji"
    if emoji_digest(emoji / "72x72") == EMOJI_HASH and all(matches(emoji / name, sha) for name, sha in EMOJI_TEXT.values()):
        return
    prefix = f"twemoji-{TWEMOJI_REV}/"
    with tarfile.open(download("twemoji.tar.gz", cache)) as archive:
        for source, (name, sha) in EMOJI_TEXT.items():
            with archive.extractfile(prefix + source) as stream:
                write_checked(emoji / name, stream.read(), sha)
        for member in archive.getmembers():
            if not member.name.startswith(prefix + "assets/72x72/"):
                continue
            name = member.name.removeprefix(prefix + "assets/72x72/")
            if member.isdir() and not name:
                continue
            if not member.isfile() or not re.fullmatch(r"[0-9a-f]+(?:-[0-9a-f]+)*\.png", name):
                raise ValueError("Unexpected emoji archive entry")
            with archive.extractfile(member) as stream:
                data = stream.read()
            write_checked(emoji / "72x72" / name, data, hashlib.sha256(data).hexdigest())
    if emoji_digest(emoji / "72x72") != EMOJI_HASH:
        raise ValueError("Emoji directory checksum mismatch; check for extra PNG files")


def prepare(output: Path, cache: Path) -> list[Path]:
    cache.mkdir(parents=True, exist_ok=True)
    prepare_fonts(output, cache)
    prepare_emoji(output, cache)
    notice = NOTICE.encode("utf-8")
    write_checked(output / "NOTICE.txt", notice, hashlib.sha256(notice).hexdigest())
    # Explicit bundle inputs exclude local manifests and development notes.
    return [output / "fonts" / name for name in (*FONT_HASHES, "OFL.txt", *(v[0] for v in JIGMO_TEXT.values()))] + [
        output / "emoji" / name for name in ("72x72", *(v[0] for v in EMOJI_TEXT.values()))] + [output / "NOTICE.txt"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "server/src/assets")
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/pdf-assets")
    args = parser.parse_args()
    try:
        prepare(args.output, args.cache)
    except (OSError, ValueError, RuntimeError, ImportError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"PDF resources unavailable: {error}\nRetry: python server/scripts/prepare-pdf-assets.py", file=sys.stderr)
        return 1
    print("PDF assets verified: 4 fonts, 4009 emoji images and notices; ready for offline export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
