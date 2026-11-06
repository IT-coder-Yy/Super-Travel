"""Render the server's escaped Markdown locally; no user HTML or remote resources."""
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path


FONT_PATH = Path(__file__).parent / "assets" / "fonts" / "NotoSansSC-Regular.ttf"
FONT_NAME = "SuperTravelSans"
FALLBACK_PATHS = tuple(FONT_PATH.parent / name for name in (
    "Jigmo-Fallback.ttf", "Jigmo2-Fallback.ttf", "Jigmo3-Fallback.ttf"))
EMOJI_PATH = Path(__file__).parent / "assets" / "emoji" / "72x72"
EMOJI_CREDIT = "Twemoji 17.0.3 - Twitter and contributors - CC BY 4.0 - github.com/jdecked/twemoji"
MAX_PDF_BYTES = 10 * 1024 * 1024


class PdfExportError(ValueError):
    pass


def _load_font(name, path):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if not path.is_file():
        raise PdfExportError("PDF 中文字体缺失，请修复安装后重试；仍可导出 Markdown。")
    class UnicodeFont(TTFont):
        def addObjects(self, doc):
            # ReportLab 5.1 encodes astral values as five hex digits, not UTF-16BE.
            # Correct only this font's CMaps; never patch the shared library globally.
            state = self.state.get(doc)
            subsets = [(self.getSubsetInternalName(index, doc)[1:], list(values))
                       for index, values in enumerate(state.subsets)] if state else []
            super().addObjects(doc)
            for internal, values in subsets:
                cmap = doc.idToObject[doc.idToObject[internal].ToUnicode.name]
                for index, value in enumerate(values):
                    if value > 0xFFFF:
                        cmap.content = cmap.content.replace(f"<{index:02X}> <{value:04X}>",
                            f"<{index:02X}> <{chr(value).encode('utf-16-be').hex().upper()}>")

    font = UnicodeFont(name, str(path))
    pdfmetrics.registerFont(font)
    return font


@lru_cache(maxsize=1)
def _font():
    return _load_font(FONT_NAME, FONT_PATH)


@lru_cache(maxsize=1)
def _fallback_fonts():
    return tuple(_load_font(f"SuperTravelFallback{index}", path) for index, path in enumerate(FALLBACK_PATHS))


@lru_cache(maxsize=1)
def _emoji_images():
    # The only image paths reachable by the renderer come from bundled hexadecimal filenames.
    images = {}
    for path in EMOJI_PATH.glob("*.png"):
        sequence = "".join(chr(int(value, 16)) for value in path.stem.split("-"))
        images[sequence.replace("\ufe0f", "")] = path
    return images


def _unsupported(character):
    raise PdfExportError(f"PDF 字体或表情资源不支持字符/序列 U+{ord(character):04X}，请调整文字或改用 Markdown 导出。")


def _markdown_blocks(markdown):
    from markdown_it import MarkdownIt
    # CommonMark otherwise replaces NUL with U+FFFD before we can report the missing content.
    for character in markdown:
        if ord(character) < 32 and character not in "\n\r\t":
            _unsupported(character)
    tokens = MarkdownIt("commonmark", {"html": False, "linkify": False}).parse(markdown)
    kind, in_list = "body", False
    blocks = []
    for token in tokens:
        if token.type == "heading_open":
            kind = {"h1": "title", "h2": "heading", "h3": "activity"}.get(token.tag)
            if kind is None:
                raise PdfExportError("PDF 行程格式无法识别，请重新生成或导出 Markdown。")
        elif token.type == "bullet_list_open":
            if in_list:
                raise PdfExportError("PDF 行程格式无法识别，请重新生成或导出 Markdown。")
            in_list = True
        elif token.type == "bullet_list_close":
            in_list = False
        elif token.type == "paragraph_open":
            kind = "item" if in_list else "body"
        elif token.type == "inline":
            text = []
            for child in token.children or []:
                if child.type == "text":
                    text.append(child.content)
                elif child.type in ("hardbreak", "softbreak"):
                    text.append("\n")
                else:
                    raise PdfExportError("PDF 行程格式无法识别，请重新生成或导出 Markdown。")
            blocks.append((kind, "".join(text)))
        elif token.type not in ("heading_close", "paragraph_close", "list_item_open", "list_item_close"):
            raise PdfExportError("PDF 行程格式无法识别，请重新生成或导出 Markdown。")
    return blocks


def _markup(text, size, emoji_used):
    import regex
    result, run, run_font = [], [], None

    def flush():
        if run:
            content = escape("".join(run), quote=False)
            result.append(content if run_font == FONT_NAME else f'<font name="{run_font}">{content}</font>')
            run.clear()

    for cluster in regex.findall(r"\X", text.expandtabs(4)):
        if cluster in ("\n", "\r", "\r\n"):
            flush()
            result.append("<br/>")
            continue
        key = cluster.replace("\ufe0f", "")
        if "\ufe0e" in cluster and (len(cluster) != 2 or cluster[0] not in _emoji_images()):
            _unsupported("\ufe0e")
        image = _emoji_images().get(key) if "\ufe0e" not in cluster else None
        is_emoji = "\ufe0e" not in cluster and (image is not None or "\ufe0f" in cluster or regex.search(r"\p{Emoji_Presentation}|\u20e3", cluster))
        if image is not None:
            if not image.is_file():
                raise PdfExportError("PDF 表情资源缺失，请修复安装后重试；仍可导出 Markdown。")
            flush()
            result.append(f'<img src="{escape(str(image), quote=True)}" width="{size}" height="{size}" valign="-2"/>')
            emoji_used.add(key)
            continue
        if is_emoji:
            _unsupported(cluster[0])
        # VS15 explicitly requests a text glyph; it is a presentation selector, not a visible character.
        for character in cluster.replace("\ufe0e", ""):
            font = _font()
            if not font.face.charToGlyph.get(ord(character)):
                font = next((value for value in _fallback_fonts() if value.face.charToGlyph.get(ord(character))), None)
            if font is None:
                _unsupported(character)
            if run_font != font.fontName:
                flush()
                run_font = font.fontName
            run.append(character)
    flush()
    return "".join(result)


def render_pdf(markdown):
    try:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate
        _font()
        blocks = _markdown_blocks(markdown)
        body = ParagraphStyle("body", fontName=FONT_NAME, fontSize=10.5, leading=16,
                              spaceAfter=6, alignment=TA_LEFT, wordWrap="CJK", splitLongWords=True)
        styles = {"body": body, "item": ParagraphStyle("item", parent=body, leftIndent=6),
                  "title": ParagraphStyle("title", parent=body, fontSize=20, leading=28, spaceAfter=14, keepWithNext=True),
                  "heading": ParagraphStyle("heading", parent=body, fontSize=14, leading=21, spaceBefore=12, spaceAfter=8, keepWithNext=True),
                  "activity": ParagraphStyle("activity", parent=body, fontSize=11.5, leading=18, spaceBefore=6, keepWithNext=True)}
        emoji_used = set()
        story = [Paragraph(_markup(text, styles[kind].fontSize, emoji_used), styles[kind]) for kind, text in blocks if text]
        output = BytesIO()
        document = SimpleDocTemplate(output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                                     topMargin=17 * mm, bottomMargin=20 * mm,
                                     title="SuperTravel 正式行程", author="SuperTravel", pageCompression=1)

        def page_number(canvas, doc):
            canvas.saveState()
            canvas.setFont(FONT_NAME, 9)
            canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, f"第 {doc.page} 页")
            if emoji_used:
                canvas.setFont(FONT_NAME, 5.5)
                canvas.drawString(18 * mm, 6 * mm, EMOJI_CREDIT)
            canvas.restoreState()

        document.build(story, onFirstPage=page_number, onLaterPages=page_number)
        result = output.getvalue()
        if len(result) > MAX_PDF_BYTES:
            raise PdfExportError("PDF 超过 10 MiB，请减少行程文字后重试，或改用 Markdown 导出。")
        return result
    except PdfExportError:
        raise
    except ImportError:
        raise PdfExportError("PDF 生成功能缺失，请修复安装后重试；仍可导出 Markdown。") from None
    except Exception:
        raise PdfExportError("PDF 生成失败，请重试或改用 Markdown 导出；已保存的行程不受影响。") from None
