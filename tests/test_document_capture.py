"""Tests for HTML / PDF / multi-page documentation capture."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

# Re-route the playbook store to a per-test temp directory.
os.environ.setdefault("PERISCRIBE_PLAYBOOK_DIR", "")


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    store = tmp_path / "playbooks"
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(store))
    yield
    if store.exists():
        shutil.rmtree(store, ignore_errors=True)


# Important: import the module AFTER the env var fixture sets the store path.
from periscribe.document_capture import (  # noqa: E402
    capture_documentation_site_to_playbook,
    capture_pdf_to_playbook,
    capture_web_page_to_playbook,
    _pick_main_content,
    _render_markdown,
    _split_into_sections,
    _strip_noise,
    _parse_html,
)
from periscribe.tutorial_playbooks import TutorialPlaybookError  # noqa: E402


SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Setup Guide for Acme Widget</title></head>
<body>
  <nav>This nav block should be dropped</nav>
  <header>Page header, dropped</header>
  <main>
    <h1>Setup Guide for Acme Widget</h1>
    <p>This guide walks you through installing and configuring the Acme Widget.</p>
    <h2>Prerequisites</h2>
    <p>You need Python 3.11 or newer and Windows 10+.</p>
    <ul>
      <li>Python 3.11+</li>
      <li>Windows 10 or later</li>
      <li>Admin rights to install</li>
    </ul>
    <h2>Install</h2>
    <p>Run the following:</p>
    <pre><code>pip install acme-widget</code></pre>
    <p>Verify the install:</p>
    <pre><code>acme-widget --version</code></pre>
    <h2>Configure</h2>
    <p>Create a config file at <code>~/.acme/config.yaml</code> and set your token.</p>
  </main>
  <footer>Copyright dropped</footer>
  <script>alert('dropped')</script>
</body>
</html>
"""


def test_strip_noise_removes_drop_tags():
    root = _parse_html(SAMPLE_HTML.encode("utf-8"))
    _strip_noise(root)
    rendered = _render_markdown(_pick_main_content(root))
    assert "nav block" not in rendered
    assert "Copyright dropped" not in rendered
    assert "alert('dropped')" not in rendered


def test_markdown_extraction_preserves_headings_and_code():
    root = _parse_html(SAMPLE_HTML.encode("utf-8"))
    _strip_noise(root)
    md = _render_markdown(_pick_main_content(root))
    # Headings preserved.
    assert "# Setup Guide for Acme Widget" in md
    assert "## Prerequisites" in md
    assert "## Install" in md
    assert "## Configure" in md
    # Code block preserved (in a fenced block).
    assert "pip install acme-widget" in md
    assert "```" in md
    # List items preserved.
    assert "- Python 3.11+" in md


def test_section_split_yields_one_per_heading():
    root = _parse_html(SAMPLE_HTML.encode("utf-8"))
    _strip_noise(root)
    md = _render_markdown(_pick_main_content(root))
    sections = _split_into_sections(md)
    headings = [h for h, _ in sections]
    assert "Setup Guide for Acme Widget" in headings
    assert "Prerequisites" in headings
    assert "Install" in headings
    assert "Configure" in headings


def test_capture_web_page_writes_playbook(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_get(self, url, *args, **kwargs):
        captured["url"] = url
        response = httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", url),
        )
        return response

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    result = capture_web_page_to_playbook(
        url="https://example.com/acme-setup",
        name="acme-widget-setup",
        summary="Acme Widget setup walkthrough.",
        overwrite=True,
    )

    assert result["ok"] is True
    assert result["slug"] == "acme-widget-setup"
    assert result["stepCount"] >= 4  # one per heading.

    pb_dir = Path(result["directory"])
    assert pb_dir.exists()
    manifest = json.loads((pb_dir / "manifest.json").read_text())
    assert manifest["sourceUrl"] == "https://example.com/acme-setup"
    assert manifest["captureConfig"]["sourceKind"] == "web_page"

    # No keyframes directory (or empty) because document sources have no images.
    assert not (pb_dir / "keyframes").exists() or not any((pb_dir / "keyframes").iterdir())

    # transcript.md exists and contains the prose without a broken image link.
    transcript = (pb_dir / "transcript.md").read_text()
    assert "Acme Widget" in transcript
    assert "![Step" not in transcript  # no image links since no keyframes.


def test_capture_web_page_rejects_non_html(monkeypatch):
    def fake_get(self, url, *args, **kwargs):
        return httpx.Response(
            200,
            content=b"%PDF-1.7 ...",
            headers={"content-type": "application/pdf"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(TutorialPlaybookError, match="did not return HTML"):
        capture_web_page_to_playbook(
            url="https://example.com/file.pdf",
            name="pdf-mistake",
            overwrite=True,
        )


def test_capture_documentation_site_crawls_linked_pages(monkeypatch):
    pages = {
        "https://docs.example.com/start": """
            <html><head><title>Start Here</title></head><body><main>
              <h1>Start Here</h1>
              <p>Welcome to the docs.</p>
              <p><a href="/docs/install">Install guide</a></p>
              <p><a href="/docs/usage">Usage</a></p>
              <p><a href="https://other.example/page">Off-site (should skip)</a></p>
            </main></body></html>
        """,
        "https://docs.example.com/docs/install": """
            <html><head><title>Install</title></head><body><main>
              <h1>Install</h1>
              <p>Run pip install foo.</p>
              <pre><code>pip install foo</code></pre>
            </main></body></html>
        """,
        "https://docs.example.com/docs/usage": """
            <html><head><title>Usage</title></head><body><main>
              <h1>Usage</h1>
              <p>Import and call.</p>
            </main></body></html>
        """,
    }

    def fake_get(self, url, *args, **kwargs):
        if url not in pages:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            content=pages[url].encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    result = capture_documentation_site_to_playbook(
        url="https://docs.example.com/start",
        name="example-docs",
        overwrite=True,
        max_pages=10,
    )

    assert result["ok"] is True
    assert result["stepCount"] >= 3  # one per page minimum

    pb_dir = Path(result["directory"])
    manifest = json.loads((pb_dir / "manifest.json").read_text())
    assert manifest["captureConfig"]["sourceKind"] == "documentation_site"
    assert manifest["captureConfig"]["pagesCaptured"] >= 3

    # No off-site pages crawled.
    transcript = (pb_dir / "transcript.md").read_text().lower()
    assert "other.example" not in transcript or "off-site" in transcript


def test_capture_pdf_writes_playbook(tmp_path):
    # Build a tiny real PDF with one text page using reportlab if available;
    # else skip cleanly. pdfplumber can read what reportlab writes.
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed; skipping live PDF test")

    pdf_path = tmp_path / "tiny.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(100, 750, "Tiny PDF Title")
    c.drawString(100, 730, "This is page one with some content.")
    c.showPage()
    c.drawString(100, 750, "Page Two")
    c.drawString(100, 730, "More content on the second page.")
    c.save()

    result = capture_pdf_to_playbook(
        path=pdf_path,
        name="tiny-pdf",
        summary="A two-page test PDF.",
        overwrite=True,
    )
    assert result["ok"] is True
    assert result["stepCount"] == 2

    pb_dir = Path(result["directory"])
    transcript = (pb_dir / "transcript.md").read_text()
    assert "Tiny PDF Title" in transcript
    assert "Page Two" in transcript


def test_capture_pdf_rejects_missing_file(tmp_path):
    missing = tmp_path / "nope.pdf"
    with pytest.raises(TutorialPlaybookError, match="PDF not found"):
        capture_pdf_to_playbook(path=missing, name="missing", overwrite=True)
