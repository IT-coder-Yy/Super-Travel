"""Real PDF bytes, embedded Chinese text, and read-only export failures."""
from base64 import b64decode
import builtins
from copy import deepcopy
from io import BytesIO
import re

from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest

from supertravel_server import exports, pdf_export
from .test_exports import export, file_state, itinerary, saved
from .test_trip_api import app, trip_result


def read_pdf(result):
    data = b64decode(result["pdfBase64"], validate=True)
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    assert len(data) <= pdf_export.MAX_PDF_BYTES
    return PdfReader(BytesIO(data))


def plain_text(pdf):
    return "\n".join(re.sub(r"第 \d+ 页\n?", "", page.extract_text()) for page in pdf.pages)


def compact(text):
    return "".join(text.split())


def long_itinerary(days):
    real = trip_result()
    prototype = real["days"][0]["activities"][0]
    real["days"] = []
    real["trip"]["summary"] = "这是保存的正式行程概述。" * 25
    for day in range(1, days + 1):
        activities = []
        for index in range(8):
            activity = deepcopy(prototype)
            title = f"第{day}天第{index + 1}处真实地点"
            activity.update(id=f"day-{day}-stop-{index}", title=title, time=f"{8 + index:02d}:00",
                            description=("长中文备注保留真实数据、不虚构费用与交通。" * 20 + f"末尾标记{day}-{index}"))
            activity["place"].update(id=f"baidu:d{day}s{index}", name=title)
            if index == 0:
                activity["cost"] = {"amountFen": 0, "currency": "CNY", "source": "user"}
            elif index == 1:
                activity["cost"] = {"amountFen": 1234, "currency": "CNY", "source": "user"}
            activities.append(activity)
        real["days"].append({"day": day, "title": f"第{day}日安排", "subtitle": "保持已确认的计划", "activities": activities})
    return real


@pytest.mark.parametrize("days", [1, 3, 7])
def test_long_chinese_pdf_matches_one_snapshot_and_embeds_fonts(app, monkeypatch, days):
    with TestClient(app) as client:
        document = saved(client, long_itinerary(days), draft=trip_result("DRAFT_PRIVATE")["days"],
                         messages=[{"id": "private", "role": "user", "content": "CHAT_PRIVATE"}],
                         candidates=[{"id": "private", "description": "CANDIDATE_PRIVATE"}],
                         modelSettings={"apiKey": "KEY_PRIVATE"})
        before = file_state(app.state.store.root)
        original_get, original_blocks = app.state.store.get, exports.export_blocks
        reads, snapshots = [], []
        def get(identifier):
            reads.append(identifier)
            return original_get(identifier)
        def blocks(*args):
            value = original_blocks(*args)
            snapshots.append(deepcopy(value))
            return value
        def forbidden(*args, **kwargs):
            pytest.fail("PDF export must not mutate storage or query travel services")
        monkeypatch.setattr(app.state.store, "get", get)
        monkeypatch.setattr(app.state.store, "mutate", forbidden)
        monkeypatch.setattr(app.state.store, "_save", forbidden)
        monkeypatch.setattr("supertravel_server.trip_routes.query_route", forbidden)
        monkeypatch.setattr(exports, "export_blocks", blocks)
        response = export(client, document, format="pdf")
        assert response.status_code == 200, response.text
        result = response.json()
        assert set(result) == {"revision", "filename", "markdown", "pdfBase64", "previewText"}
        assert result["filename"] == "杭州-行程.pdf" and result["revision"] == document["revision"]
        assert len(reads) == len(snapshots) == 1
        assert result["previewText"] == "\n\n".join(text for _, text in snapshots[0])
        pdf = read_pdf(result)
        assert len(pdf.pages) >= 2
        extracted = plain_text(pdf)
        for _, text in snapshots[0]:
            assert compact(text) in compact(extracted), text
        assert "PRIVATE" not in extracted and "sourceLocation" not in extracted and "baidu:fixturepoi" not in extracted
        assert "免费，用户填写" in extracted and f"费用未知项：{days * 6} 项" in extracted
        assert "无完整路线记录" in compact(extracted) and "不代表完整旅行总价或已核验票价" in compact(extracted)
        for number, page in enumerate(pdf.pages, 1):
            assert f"第 {number} 页" in page.extract_text()
            assert float(page.mediabox.width) == pytest.approx(595.28, abs=0.02)
            assert float(page.mediabox.height) == pytest.approx(841.89, abs=0.02)
            fonts = [value.get_object() for value in page["/Resources"]["/Font"].values()]
            embedded = [font for font in fonts if "NotoSansSC" in font.get("/BaseFont", "")]
            assert embedded and all(font["/FontDescriptor"]["/FontFile2"].get_data() for font in embedded)
        assert file_state(app.state.store.root) == before


def test_pdf_escapes_xml_and_wraps_long_urls_as_inert_text(app, monkeypatch):
    with TestClient(app) as client:
        real = itinerary()
        payload = '<img src="https://invalid.example/image"/><script>alert(1)</script> & <b>原样</b> '
        long_word = "https://example.test/" + "x" * 280
        real["days"][0]["activities"][0]["description"] = payload + "\n" + long_word
        real["trip"]["summary"] = '<para color="red">不是格式标签</para> &lt; # [链接](https://example.test)'
        def forbidden(*args, **kwargs):
            pytest.fail("PDF must not load external resources")
        monkeypatch.setattr("urllib.request.urlopen", forbidden)
        response = export(client, saved(client, real), format="pdf")
        assert response.status_code == 200, response.text
        pdf = read_pdf(response.json())
        text = compact(plain_text(pdf))
        assert compact(payload) in text and long_word in text
        assert compact(real["trip"]["summary"]) not in text
        assert compact("2 天行程；第 1 天 3 项活动；第 2 天自由安排。") in text
        assert all("/Annots" not in page for page in pdf.pages)
        assert not any(key in pdf.trailer["/Root"] for key in ("/OpenAction", "/AA", "/AcroForm", "/Names"))


@pytest.mark.parametrize("failure", ["font", "dependency", "generation", "size"])
def test_pdf_failure_is_clear_read_only_and_retryable_with_markdown_available(app, monkeypatch, tmp_path, failure):
    with TestClient(app) as client:
        document = saved(client)
        before = file_state(app.state.store.root)
        pdf_export._font.cache_clear()
        with monkeypatch.context() as patch:
            if failure == "font":
                patch.setattr(pdf_export, "FONT_PATH", tmp_path / "missing.ttf")
            elif failure == "dependency":
                original_import = builtins.__import__
                def missing(name, *args, **kwargs):
                    if name.startswith("reportlab"):
                        raise ImportError("INTERNAL_PATH")
                    return original_import(name, *args, **kwargs)
                patch.setattr(builtins, "__import__", missing)
            elif failure == "generation":
                def fail(*args, **kwargs):
                    raise RuntimeError("INTERNAL_PATH")
                patch.setattr("reportlab.platypus.SimpleDocTemplate.build", fail)
            else:
                patch.setattr(pdf_export, "MAX_PDF_BYTES", 16)
            response = export(client, document, format="pdf")
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "trip_pdf_unavailable"
            assert "Markdown" in response.text and "INTERNAL_PATH" not in response.text
            assert "pdfBase64" not in response.json()
            assert export(client, document).status_code == 200
            assert file_state(app.state.store.root) == before
        pdf_export._font.cache_clear()
        retry = export(client, document, format="pdf")
        assert retry.status_code == 200, retry.text
        assert read_pdf(retry.json()).pages
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("character", ["\U0010ffff", "\x00"])
def test_unsupported_glyph_or_control_is_reported_not_silently_dropped(app, character):
    with TestClient(app) as client:
        real = trip_result("前文" + character + "后文")
        document = saved(client, real)
        before = file_state(app.state.store.root)
        response = export(client, document, format="pdf")
        assert response.status_code == 422
        assert f"U+{ord(character):04X}" in response.text and "Markdown" in response.text
        assert export(client, document).status_code == 200
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("value", ["html", "PDF", None, True, 1])
def test_pdf_format_is_strict(app, value):
    with TestClient(app) as client:
        response = export(client, saved(client), format=value)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_request"


def test_pdf_does_not_accept_client_document_or_unauthenticated_access(app):
    with TestClient(app) as client:
        document = saved(client)
        response = export(client, document, format="pdf", markdown="CLIENT_CONTENT", pdfBase64="CLIENT_BYTES")
        assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_request"
        response = client.post(f"/api/conversations/{document['id']}/trip/export",
                               json={"expected_revision": document["revision"], "format": "pdf"})
        assert response.status_code == 401


def test_concurrent_exports_keep_text_and_font_subsets_separate():
    from concurrent.futures import ThreadPoolExecutor
    values = ["杭州西湖", "北京故宫", "成都武侯祠", "西安古城墙"]
    def render(value):
        data = pdf_export.render_pdf(exports._markdown([("title", value), ("body", "中文字体并发导出")]))
        return plain_text(PdfReader(BytesIO(data)))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(render, values))
    for value, text in zip(values, results):
        assert value in text and all(other not in text for other in values if other != value)



def test_pdf_uses_exact_exported_markdown_and_preserves_literal_formatting(app, monkeypatch):
    with TestClient(app) as client:
        real = trip_result("第一行\n===\n# * 标题不是格式\n\n最后 &lt; &amp; <b>原文</b>")
        seen = []
        original = pdf_export.render_pdf
        def render(markdown):
            seen.append(markdown)
            return original(markdown)
        monkeypatch.setattr(pdf_export, "render_pdf", render)
        response = export(client, saved(client, real), format="pdf")
        assert response.status_code == 200, response.text
        result = response.json()
        assert seen == [result["markdown"]]
        assert compact(real["days"][0]["activities"][0]["description"]) in compact(plain_text(read_pdf(result)))


def test_rare_characters_and_whole_emoji_sequences_are_rendered_locally(app, monkeypatch):
    from PIL import Image
    text = "罕字：𠮷𠮶鿿𰻞𱍐；表情：😀👍🏽👩‍💻🇨🇳1️⃣❤️👨‍👩‍👧‍👦；尾部备注"
    files = ["1f600.png", "1f44d-1f3fd.png", "1f469-200d-1f4bb.png", "1f1e8-1f1f3.png",
             "31-20e3.png", "2764.png", "1f468-200d-1f469-200d-1f467-200d-1f466.png"]
    with TestClient(app) as client:
        document = saved(client, trip_result(text))
        before = file_state(app.state.store.root)
        def forbidden(*args, **kwargs):
            pytest.fail("Unicode PDF must not use network resources")
        monkeypatch.setattr("urllib.request.urlopen", forbidden)
        response = export(client, document, format="pdf")
        assert response.status_code == 200, response.text
        result = response.json()
        assert text in result["markdown"] and text in result["previewText"]
        pdf = read_pdf(result)
        extracted = compact(plain_text(pdf))
        assert "罕字：𠮷𠮶鿿𰻞𱍐；表情：；尾部备注" in extracted
        assert pdf_export.EMOJI_CREDIT in plain_text(pdf)
        images = [image.image.convert("RGBA").tobytes() for page in pdf.pages for image in page.images]
        for filename in files:
            with Image.open(pdf_export.EMOJI_PATH / filename) as source:
                assert source.convert("RGBA").tobytes() in images, filename
        fonts = [font.get_object() for page in pdf.pages for font in page["/Resources"]["/Font"].values()]
        for family in ("Jigmo", "Jigmo2", "Jigmo3"):
            matches = [font for font in fonts if font.get("/BaseFont", "").split("+")[-1] == family]
            assert matches and all(font["/FontDescriptor"]["/FontFile2"].get_data() for font in matches)
        assert file_state(app.state.store.root) == before


def test_supplementary_characters_survive_multiple_font_subsets_and_concurrent_documents():
    from concurrent.futures import ThreadPoolExecutor
    font = pdf_export._fallback_fonts()[1]
    characters = "".join(chr(code) for code in sorted(font.face.charToGlyph) if 0x20000 <= code <= 0x2A6DF)[:300]
    assert len(characters) == 300
    def render(marker):
        data = pdf_export.render_pdf(exports._markdown([("title", marker), ("body", characters)]))
        pdf = PdfReader(BytesIO(data))
        fonts = {font.get_object()["/BaseFont"] for page in pdf.pages for font in page["/Resources"]["/Font"].values()}
        assert len([name for name in fonts if "Jigmo2" in name]) >= 2
        return plain_text(pdf)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(render, ["杭州并发", "北京并发", "成都并发"]))
    for marker, text in zip(["杭州并发", "北京并发", "成都并发"], results):
        assert marker in text and characters in compact(text)


@pytest.mark.parametrize("text", ["前文\U0001f600\u200dX后文", "前文\ufe0e后文", "前文𠮷\U000e0100后文"])
def test_unsupported_sequences_do_not_silently_drop_joiners_or_selectors(text):
    with pytest.raises(pdf_export.PdfExportError):
        pdf_export.render_pdf(exports._markdown([("body", text)]))


def test_text_presentation_selector_is_not_replaced_by_color_emoji():
    data = pdf_export.render_pdf(exports._markdown([("body", "文本版权 ©︎ 保留")]))
    pdf = PdfReader(BytesIO(data))
    assert "文本版权 © 保留" in plain_text(pdf)
    assert not pdf.pages[0].images and pdf_export.EMOJI_CREDIT not in plain_text(pdf)


def test_missing_emoji_asset_fails_without_silently_replacing_it(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf_export, "_emoji_images", lambda: {"😀": tmp_path / "missing.png"})
    with pytest.raises(pdf_export.PdfExportError, match="表情资源缺失"):
        pdf_export.render_pdf(exports._markdown([("body", "😀")]))


@pytest.mark.parametrize("markdown", ["![外部图片](https://invalid.example/a.png)", "<img src='file:///private.png'>", "[链接](https://invalid.example)", "```html\n<p>任意结构</p>\n```"])
def test_renderer_does_not_load_raw_markdown_resources_or_unexpected_tokens(markdown, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No external resource loader may be reached")
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    if markdown.startswith("<"):
        text = plain_text(PdfReader(BytesIO(pdf_export.render_pdf(markdown))))
        assert markdown in text
    else:
        with pytest.raises(pdf_export.PdfExportError):
            pdf_export.render_pdf(markdown)
