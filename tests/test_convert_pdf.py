from __future__ import annotations

import pytest

from agent_web import detect
from agent_web.convert.pdf import PdfConversion, PdfUnreadable, convert_pdf
from agent_web.model import PdfLayoutWarning, ScannedPdf
from conftest import make_pdf


def test_pages_get_markers() -> None:
    result = convert_pdf(make_pdf(["Alpha page", "Beta page"]))
    assert isinstance(result, PdfConversion)
    assert result.pages == 2
    assert result.markdown.index("--- Page 1 ---") < result.markdown.index("Alpha page")
    assert result.markdown.index("--- Page 2 ---") < result.markdown.index("Beta page")
    assert result.text_chars >= len("AlphapageBetapage")


def test_garbage_is_unreadable() -> None:
    assert isinstance(convert_pdf(b"%PDF-1.7 this is not really a pdf"), PdfUnreadable)
    assert isinstance(convert_pdf(b""), PdfUnreadable)


def test_textless_pdf_is_flagged_as_scanned() -> None:
    result = convert_pdf(make_pdf(["", ""]))
    assert isinstance(result, PdfConversion)
    issues = detect.pdf_issues(status=200, pages=result.pages, text_chars=result.text_chars)
    assert ScannedPdf() in issues
    assert PdfLayoutWarning() in issues


def test_converter_output_does_not_leak_to_stdout(capfd: pytest.CaptureFixture[str]) -> None:
    convert_pdf(make_pdf(["Quiet please"]))
    assert capfd.readouterr().out == ""
