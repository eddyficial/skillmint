"""Capture written training material (web pages, PDFs, documentation sites) into
the same playbook structure that ``offline_video_capture`` produces. Distill /
scaffold / codify don't care where the playbook came from.

Public entry points
-------------------
- ``capture_web_page_to_playbook(url, name, ...)`` — single HTML page
- ``capture_pdf_to_playbook(path, name, ...)`` — local PDF (or fetched URL)
- ``capture_documentation_site_to_playbook(url, name, max_pages, ...)`` —
  same-origin BFS crawl of a docs site
"""

from __future__ import annotations

import io
import re
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import pdfplumber
from lxml import etree, html as lxml_html

from skillmint.tutorial_playbooks import (
    TutorialPlaybookError,
    persist_playbook_from_snapshot,
)


# ---------------------------------------------------------------------------
# Tag stripping & content extraction
# ---------------------------------------------------------------------------

_DROP_TAGS = {
    "script", "style", "nav", "footer", "header", "aside", "form",
    "iframe", "noscript", "button", "input", "select", "textarea", "svg",
}
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "li", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_WHITESPACE = re.compile(r"[ \t]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_HTTP_DEFAULT_TIMEOUT = 30.0
_HTTP_DEFAULT_USER_AGENT = (
    "Skillmint/0.1 (+https://skillmint.ai) - tutorial capture for LLM agents"
)


def _fetch_url(url: str, *, timeout: float = _HTTP_DEFAULT_TIMEOUT) -> tuple[str, str, bytes]:
    """GET a URL, return (final_url, content_type, body_bytes). Follows redirects."""
    headers = {"User-Agent": _HTTP_DEFAULT_USER_AGENT, "Accept": "*/*"}
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        response = client.get(url)
        response.raise_for_status()
        return str(response.url), response.headers.get("content-type", ""), response.content


def _parse_html(html_bytes: bytes) -> lxml_html.HtmlElement:
    return lxml_html.fromstring(html_bytes)


def _strip_noise(root: lxml_html.HtmlElement) -> None:
    """Mutate the tree in place — drop nav/footer/script/etc."""
    for tag in _DROP_TAGS:
        for el in root.iter(tag):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


def _pick_main_content(root: lxml_html.HtmlElement) -> lxml_html.HtmlElement:
    """Return the subtree most likely to be the article body.

    Strategy: prefer explicit <main> / <article> / role="main"; else pick the
    descendant of <body> with the most cumulative <p> text length.
    """
    for selector in ("//main", "//article", "//*[@role='main']", "//*[@id='content']", "//*[@id='main']"):
        try:
            matches = root.xpath(selector)
        except etree.XPathEvalError:
            matches = []
        if matches:
            return matches[0]

    body = root.find(".//body") if root.tag != "body" else root
    candidate = body if body is not None else root
    best = candidate
    best_score = _text_density_score(candidate)
    for div in candidate.iter("div", "section"):
        score = _text_density_score(div)
        if score > best_score:
            best = div
            best_score = score
    return best


def _text_density_score(el: lxml_html.HtmlElement) -> int:
    score = 0
    for p in el.iter("p"):
        text = " ".join(p.itertext()).strip()
        score += len(text)
    return score


def _render_markdown(root: lxml_html.HtmlElement) -> str:
    """Walk a cleaned HTML element and emit a markdown-ish representation."""
    out: list[str] = []
    _walk(root, out)
    text = "\n".join(out)
    text = _MULTI_NEWLINE.sub("\n\n", text).strip()
    return text


def _walk(el: lxml_html.HtmlElement, out: list[str]) -> None:
    tag = (el.tag or "").lower() if isinstance(el.tag, str) else ""
    if tag in _DROP_TAGS:
        return

    if tag in _HEADING_TAGS:
        level = int(tag[1])
        text = _inline_text(el)
        if text:
            out.append("")
            out.append("#" * level + " " + text)
            out.append("")
        return

    if tag == "pre":
        text = "".join(el.itertext()).rstrip()
        if text:
            out.append("")
            out.append("```")
            out.append(text)
            out.append("```")
            out.append("")
        return

    if tag == "code" and (el.getparent() is None or el.getparent().tag != "pre"):
        text = _inline_text(el)
        if text:
            out.append(f"`{text}`")
        return

    if tag == "li":
        text = _inline_text(el)
        if text:
            out.append(f"- {text}")
        return

    if tag in {"p", "blockquote"}:
        text = _inline_text(el)
        if text:
            prefix = "> " if tag == "blockquote" else ""
            out.append("")
            out.append(prefix + text)
            out.append("")
        return

    # Recurse into containers (div, section, body, etc.)
    if el.text:
        cleaned = _WHITESPACE.sub(" ", el.text).strip()
        if cleaned and tag not in _BLOCK_TAGS:
            out.append(cleaned)
    for child in el:
        _walk(child, out)
        if child.tail:
            cleaned = _WHITESPACE.sub(" ", child.tail).strip()
            if cleaned:
                out.append(cleaned)


def _inline_text(el: lxml_html.HtmlElement) -> str:
    parts: list[str] = []
    for txt in el.itertext():
        parts.append(txt)
    return _WHITESPACE.sub(" ", " ".join(parts)).strip()


def _split_into_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown by headings into (heading, body) sections."""
    sections: list[tuple[str, str]] = []
    current_heading: str = ""
    current_lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("#"):
            if current_lines or current_heading:
                body = "\n".join(current_lines).strip()
                sections.append((current_heading, body))
            current_heading = line.lstrip("# ").strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines or current_heading:
        body = "\n".join(current_lines).strip()
        sections.append((current_heading, body))
    return [s for s in sections if s[0] or s[1]]


# ---------------------------------------------------------------------------
# Snapshot shaping
# ---------------------------------------------------------------------------


def _make_step(
    *,
    ordinal: int,
    text: str,
    heading: str | None = None,
    source_url: str | None = None,
    page_number: int | None = None,
) -> dict[str, Any]:
    """Shape a step record compatible with persist_playbook_from_snapshot."""
    label_bits: list[str] = []
    if heading:
        label_bits.append(heading)
    if page_number is not None:
        label_bits.append(f"page {page_number}")
    if source_url:
        label_bits.append(source_url)
    caption = "\n\n".join([" / ".join(label_bits), text]) if label_bits else text
    return {
        "sequence": ordinal,
        "startedAt": None,
        "endedAt": None,
        "trigger": "document_section",
        "diffScore": 100.0,  # Force section break in distill.
        "videoStartSeconds": None,
        "videoEndSeconds": None,
        "keyframeJpeg": b"",
        "keyframeWidth": None,
        "keyframeHeight": None,
        "transcriptText": text,
        "captionText": caption,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public capture functions
# ---------------------------------------------------------------------------


def capture_web_page_to_playbook(
    url: str,
    name: str,
    *,
    summary: str | None = None,
    overwrite: bool = False,
    timeout_seconds: float = _HTTP_DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Fetch a single HTML page, extract main content, write a playbook.

    The page is split into one step per heading. The playbook ends up readable
    by distill/scaffold/codify exactly like a YouTube playbook.
    """
    if not url:
        raise TutorialPlaybookError("url is required")

    final_url, content_type, body = _fetch_url(url, timeout=timeout_seconds)
    if "html" not in content_type.lower() and not body.lstrip().startswith(b"<"):
        raise TutorialPlaybookError(
            f"URL did not return HTML (content-type: {content_type}); "
            "use capture_pdf_to_playbook for PDFs."
        )

    root = _parse_html(body)
    _strip_noise(root)
    main = _pick_main_content(root)
    markdown = _render_markdown(main)
    sections = _split_into_sections(markdown)
    if not sections:
        raise TutorialPlaybookError(
            "no readable content extracted from the page; "
            "consider capturing as PDF or using documentation_site capture for JS-rendered sites"
        )

    page_title = _page_title(root)
    steps = [
        _make_step(ordinal=idx + 1, text=body_text, heading=heading, source_url=final_url)
        for idx, (heading, body_text) in enumerate(sections)
        if body_text or heading
    ]
    snapshot = {
        "sessionId": None,
        "url": final_url,
        "video": {"title": page_title, "channel": _origin_label(final_url)},
        "config": {"sourceKind": "web_page", "originalUrl": url, "fetchedAt": _now_iso()},
        "steps": steps,
        "fullTranscriptText": markdown,
    }
    return persist_playbook_from_snapshot(
        name=name,
        snapshot=snapshot,
        overwrite=overwrite,
        summary=summary,
    )


def capture_pdf_to_playbook(
    path: str | Path,
    name: str,
    *,
    summary: str | None = None,
    overwrite: bool = False,
    page_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Extract text from a local PDF and persist as a playbook.

    One step per PDF page. For very long PDFs (200+ pages), pass page_range
    to slice. Use distill_tutorial_playbook to re-group pages into topical
    sections.
    """
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise TutorialPlaybookError(f"PDF not found: {pdf_path}")

    steps: list[dict[str, Any]] = []
    full_text_parts: list[str] = []
    pdf_title: str = pdf_path.stem

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        if page_range is not None:
            start, end = page_range
            pages = pdf.pages[start - 1 : end]
            start_page_number = start
        else:
            start_page_number = 1
        for i, page in enumerate(pages):
            page_no = start_page_number + i
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            full_text_parts.append(f"## Page {page_no}\n\n{text}")
            steps.append(
                _make_step(
                    ordinal=len(steps) + 1,
                    text=text,
                    heading=f"Page {page_no}",
                    page_number=page_no,
                    source_url=pdf_path.as_uri(),
                )
            )
        metadata = pdf.metadata or {}
        if metadata.get("Title"):
            pdf_title = str(metadata["Title"]).strip() or pdf_title

    if not steps:
        raise TutorialPlaybookError(
            "no text extracted from PDF (scanned image-only PDF? OCR support not implemented)"
        )

    snapshot = {
        "sessionId": None,
        "url": pdf_path.as_uri(),
        "video": {"title": pdf_title, "channel": pdf_path.parent.name or "PDF"},
        "config": {
            "sourceKind": "pdf",
            "sourcePath": str(pdf_path),
            "pageCount": len(steps),
            "fetchedAt": _now_iso(),
        },
        "steps": steps,
        "fullTranscriptText": "\n\n".join(full_text_parts),
    }
    return persist_playbook_from_snapshot(
        name=name,
        snapshot=snapshot,
        overwrite=overwrite,
        summary=summary,
    )


def capture_documentation_site_to_playbook(
    url: str,
    name: str,
    *,
    summary: str | None = None,
    overwrite: bool = False,
    max_pages: int = 30,
    same_origin_only: bool = True,
    url_pattern: str | None = None,
    timeout_seconds: float = _HTTP_DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """BFS crawl a documentation site starting from `url`, up to max_pages.

    Each fetched page becomes one or more steps (one per heading on the page).
    Use `url_pattern` (substring or regex) to constrain which links are followed.
    """
    if not url:
        raise TutorialPlaybookError("url is required")
    if max_pages < 1:
        raise TutorialPlaybookError("max_pages must be >= 1")

    pattern_re = re.compile(url_pattern) if url_pattern else None
    seed_origin = _origin(url)

    visited: set[str] = set()
    queue: deque[str] = deque([url])
    captured_pages: list[dict[str, Any]] = []  # {url, title, sections}
    failures: list[dict[str, str]] = []

    while queue and len(captured_pages) < max_pages:
        current = queue.popleft()
        normalized = _normalize_url(current)
        if normalized in visited:
            continue
        visited.add(normalized)

        try:
            final_url, content_type, body = _fetch_url(current, timeout=timeout_seconds)
        except Exception as exc:
            failures.append({"url": current, "error": str(exc)})
            continue
        if "html" not in content_type.lower():
            continue

        root = _parse_html(body)
        _strip_noise(root)
        main = _pick_main_content(root)
        markdown = _render_markdown(main)
        sections = _split_into_sections(markdown)
        if not sections:
            continue
        page_title = _page_title(root)
        captured_pages.append(
            {"url": final_url, "title": page_title, "sections": sections}
        )

        for link in _extract_links(main, final_url):
            if same_origin_only and _origin(link) != seed_origin:
                continue
            if pattern_re and not pattern_re.search(link):
                continue
            norm = _normalize_url(link)
            if norm not in visited:
                queue.append(link)

    if not captured_pages:
        raise TutorialPlaybookError(
            f"crawl produced no usable pages (failures: {failures})"
        )

    steps: list[dict[str, Any]] = []
    full_text_parts: list[str] = []
    for page in captured_pages:
        for heading, body_text in page["sections"]:
            if not (heading or body_text):
                continue
            steps.append(
                _make_step(
                    ordinal=len(steps) + 1,
                    text=body_text,
                    heading=f"{page['title']} — {heading}" if heading else page["title"],
                    source_url=page["url"],
                )
            )
            full_text_parts.append(f"## {page['title']} — {heading}\n\n{body_text}")

    if not steps:
        raise TutorialPlaybookError("crawl pages had headings but no usable body text")

    snapshot = {
        "sessionId": None,
        "url": url,
        "video": {
            "title": _page_title(_parse_html(_fetch_url(url, timeout=timeout_seconds)[2]))
                if captured_pages else url,
            "channel": _origin_label(url),
        },
        "config": {
            "sourceKind": "documentation_site",
            "seedUrl": url,
            "pagesCaptured": len(captured_pages),
            "maxPages": max_pages,
            "sameOriginOnly": same_origin_only,
            "urlPattern": url_pattern,
            "failures": failures,
            "fetchedAt": _now_iso(),
        },
        "steps": steps,
        "fullTranscriptText": "\n\n".join(full_text_parts),
    }
    return persist_playbook_from_snapshot(
        name=name,
        snapshot=snapshot,
        overwrite=overwrite,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------


def _extract_links(el: lxml_html.HtmlElement, base_url: str) -> Iterable[str]:
    seen: set[str] = set()
    for a in el.iter("a"):
        href = a.get("href")
        if not href:
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        # Drop fragments — same-page anchors aren't new content.
        without_frag = absolute.split("#", 1)[0]
        if not without_frag.startswith(("http://", "https://")):
            continue
        if without_frag in seen:
            continue
        seen.add(without_frag)
        yield without_frag


def _origin(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _origin_label(url: str) -> str:
    return urllib.parse.urlparse(url).netloc or url


def _normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.split("#", 1)[0])
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def _page_title(root: lxml_html.HtmlElement) -> str:
    title_el = root.find(".//title")
    if title_el is not None and title_el.text:
        return title_el.text.strip()
    h1 = root.find(".//h1")
    if h1 is not None:
        text = _inline_text(h1)
        if text:
            return text
    return ""
