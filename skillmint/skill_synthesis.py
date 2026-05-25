"""Compose a Claude Code skill scaffold from a tutorial playbook.

A tutorial playbook is captured knowledge (what a teacher said and showed);
a Claude Code skill is reusable instructions for an agent to do something.
This module bridges the two by writing a SKILL.md scaffold under
``.claude/skills/<slug>/`` populated with the playbook's distilled sections
and a stubbed-out "How to apply" block. The current Claude session then
edits "How to apply" into a real procedure via the /codify skill.

Splitting scaffold-creation from synthesis keeps the LLM cost in the
already-running Claude Code session (no separate API key, no per-call
billing) while still letting the agent ask clarifying questions during
codification.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .tutorial_playbooks import _playbook_dir, _slugify

_lock = threading.RLock()


# Source-kind constants. YouTube playbooks don't set captureConfig.sourceKind
# (they use captureMode), so the absence of a sourceKind is treated as video.
_SOURCE_YOUTUBE = "youtube_video"
_SOURCE_WEB = "web_page"
_SOURCE_PDF = "pdf"
_SOURCE_DOCS = "documentation_site"


class SkillSynthesisError(RuntimeError):
    """Raised when a skill scaffold cannot be composed."""


def _skill_dir(skill_slug: str, *, base: Path | None = None) -> Path:
    """Return the on-disk location for a project-local skill.

    ``base`` may be either a project root (in which case ``.claude/skills`` is
    appended) or a path that already ends in ``.claude/skills`` (in which case
    the slug is appended directly). Both forms produce the same final path.
    """
    root = base if base is not None else Path.cwd()
    parts = root.parts[-2:]
    if len(parts) == 2 and parts[-2] == ".claude" and parts[-1] == "skills":
        return root / skill_slug
    return root / ".claude" / "skills" / skill_slug


def _mm_ss(seconds: float | int | None) -> str:
    """Render a seconds value as M:SS for compact section labels."""
    if seconds is None:
        return "?:??"
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _truncate_words(text: str, max_words: int) -> str:
    """Return the first ``max_words`` words of ``text`` (no trailing ellipsis if shorter)."""
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + "..."


def _source_kind(manifest: dict[str, Any]) -> str:
    """Determine the source kind from the manifest.

    Document captures (web/pdf/docs) set ``captureConfig.sourceKind`` explicitly;
    YouTube captures don't (they predate the field and set ``captureMode`` instead),
    so an absent sourceKind is treated as video.
    """
    config = manifest.get("captureConfig") or {}
    kind = config.get("sourceKind")
    return kind if kind else _SOURCE_YOUTUBE


# Marketing/branding suffixes that bloat titles and hurt trigger matching.
# Pattern order matters: longest/most-specific first.
_TITLE_STRIP_PATTERNS: tuple[str, ...] = (
    r"\s*[—\-]\s*MDN\s*$",
    r"\s*\|\s*MDN\s*$",
    r"\s+in\s+\d+\s+(?:Seconds?|Minutes?|Hours?)\s*$",
    r"\s*FULL COURSE\s*",
    r"\s*Complete (?:Tutorial|Course|Guide)\s*",
    r"\s*Tutorial for Beginners?\s*",
    r"\s*for Beginners?\s*",
    r"\s*Tutorial\s*$",
    r"\s*Course\s*$",
    r"\s*Guide\s*$",
    r"\s*\[\d{4}\]\s*",
    r"\s*\(\d{4}\)\s*",
)


_GENERIC_DOC_TITLES: frozenset[str] = frozenset({
    "getting started", "introduction", "overview", "home", "welcome", "docs",
    "documentation", "readme", "index", "first steps", "quickstart", "quick start",
})

_GENERIC_PATH_SEGMENTS: frozenset[str] = frozenset({
    "docs", "doc", "documentation", "getting-started", "quickstart", "quick-start",
    "intro", "introduction", "guides", "guide", "reference", "api", "manual",
    "en", "en-us", "latest", "stable",
})


def _topic_from_seed_url(seed_url: str) -> str:
    """Pull the project name from a docs-site seed URL.

    Most docs sites follow ``docs.<product>.<tld>/<product>/<section>/`` or
    ``<vendor>.com/<product>/docs/<section>/``. We walk path segments left-to-right
    and pick the first one that isn't a generic doc-routing segment.
    """
    if not seed_url:
        return ""
    # Strip scheme + host.
    after_scheme = re.sub(r"^https?://", "", seed_url, flags=re.IGNORECASE)
    host_and_path = after_scheme.split("/", 1)
    if len(host_and_path) < 2:
        return ""
    path_segments = [s for s in host_and_path[1].split("/") if s]
    for seg in path_segments:
        if seg.lower() not in _GENERIC_PATH_SEGMENTS:
            # uv, anthropic-api, claude-code → looks like product/topic.
            return seg.replace("-", " ").replace("_", " ").strip()
    return ""


def _topic_from_title(title: str) -> str:
    """Strip common marketing/branding suffixes to leave a short matchable topic."""
    t = (title or "").strip()
    for pat in _TITLE_STRIP_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    # Title splits on common separators; keep the most distinctive (usually first) chunk.
    for sep in (" | ", " — ", " - "):
        if sep in t:
            parts = [p.strip() for p in t.split(sep) if p.strip()]
            # Prefer the longest part — it usually has the actual topic.
            if parts:
                t = max(parts, key=len)
            break
    return t.strip() or (title or "").strip()


def _extract_doc_heading(text: str) -> str:
    """Pull the heading off the start of a document_section text.

    The capture format puts ``<heading> / <URL> <body>`` for web/docs sections
    and ``Page N / page N / file://... <body>`` for PDFs. This returns just the
    leading heading chunk (and, for docs nested as ``Page Title — Section``,
    the section after the em-dash).
    """
    if not text:
        return ""
    first = text.split(" / ", 1)[0].strip()
    if " — " in first:
        first = first.split(" — ", 1)[1].strip()
    return first


def _pdf_page_number(text: str, fallback_ordinal: int | str) -> str:
    """Extract the printed page number off the start of a PDF section's text."""
    if text:
        m = re.match(r"^Page\s+(\d+)\b", text)
        if m:
            return m.group(1)
    return str(fallback_ordinal)


# Small stopword set — enough to keep keyword extraction from emitting filler.
# YouTube outro spam ("subscribe, channel, thanks for watching, like, comment")
# is filtered too, otherwise short videos like Fireship 100-Second clips have
# their outros dominate the bigram counts.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "with", "from", "this", "that", "these", "those",
    "your", "have", "has", "had", "but", "not", "all", "any", "can", "you",
    "use", "will", "into", "out", "more", "when", "what", "which", "how", "why",
    "about", "also", "some", "such", "only", "own", "than", "too", "very", "just",
    "now", "page", "section", "their", "his", "her", "its", "our", "via", "see",
    "should", "would", "could", "may", "might", "must", "been", "being", "was",
    "were", "they", "them", "then", "there", "here", "each", "few", "most", "other",
    "same", "between", "before", "after", "above", "below", "during", "through",
    "over", "under", "again", "further", "lit", "node", "part", "video", "tutorial",
    "lesson", "topic", "intro", "introduction", "overview", "summary", "note", "tip",
    "subscribe", "channel", "thanks", "watching", "watch", "like", "comment", "share",
    "tuned", "stay", "coming", "soon", "hey", "guys", "welcome", "today",
    "let", "lets", "make", "sure", "thing", "things", "really", "okay", "want",
    "going", "gonna", "going-to",
    # Generic fillers that survive token-level pruning but read as noise in a Covers list.
    "one", "two", "three", "four", "five", "first", "second", "third", "another",
    "many", "much", "way", "ways", "kind", "kinds", "lot", "lots",
    "reliably", "easily", "quickly", "simply", "actually", "basically",
    "needed", "need", "needs", "good", "great", "better", "best",
})

# URL-ish tokens to strip from the bag-of-words before keyword extraction.
# Without this, MDN/docs sections (which embed the source URL in their text)
# emit keywords like "https developer" and "en-us docs".
_URL_RE = re.compile(r"https?://\S+|file:///\S+|www\.\S+")
_URL_FRAGMENT_TOKENS: frozenset[str] = frozenset({
    "https", "http", "file", "www", "com", "org", "net", "io", "dev", "html",
    "htm", "php", "asp", "aspx", "pdf", "docx", "xlsx",
})


def _section_titles_and_bodies(
    sections: list[dict[str, Any]], source_kind: str
) -> tuple[list[str], list[str]]:
    """Split section text into (titles, body-first-sentences) per source kind.

    For document sources the title is the extracted heading (always present).
    For video the title is empty; the body excerpt carries the signal.
    """
    titles: list[str] = []
    bodies: list[str] = []
    for s in sections:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if source_kind != _SOURCE_YOUTUBE:
            heading = _extract_doc_heading(text)
            if heading:
                titles.append(heading)
            # Body for doc sources = text after the URL chunk.
            after = text.split(" / ", 2)
            body = after[-1] if len(after) > 1 else text
        else:
            body = text
        # Strip URLs before sentence splitting — file:// URLs end in '.pdf' etc.
        # and would otherwise consume the entire first-sentence slice.
        body = _URL_RE.sub(" ", body)
        # First "sentence" — up to first period or 25 words, whichever comes first.
        first_sentence = body.split(".", 1)[0]
        bodies.append(" ".join(first_sentence.split()[:25]))
    return titles, bodies


def _keywords(sections: list[dict[str, Any]], source_kind: str, *, max_kw: int = 5) -> list[str]:
    """Return up to ``max_kw`` informative keywords (bigrams preferred) from the sections.

    Uses pure frequency analysis with a stopword filter — no LLM. Bigrams with
    count ≥ 2 are prioritized over unigrams since bigrams carry more topical
    signal ("data analyst" vs. "data"). Document sources prefer headings;
    video sources fall back to first-sentence body text.
    """
    titles, bodies = _section_titles_and_bodies(sections, source_kind)
    # Headings carry the strongest signal — weight them by repeating.
    candidates = (titles * 2) + bodies if titles else bodies
    blob = " ".join(candidates).lower()
    # Drop URLs entirely before tokenizing so URL fragments can't become bigrams.
    blob = _URL_RE.sub(" ", blob)
    tokens = re.findall(r"[a-z][a-z0-9\-]{2,}", blob)
    tokens = [
        t for t in tokens
        if t not in _STOPWORDS and t not in _URL_FRAGMENT_TOKENS and not t.isdigit()
    ]

    unigrams = Counter(tokens)
    bigrams: Counter[str] = Counter()
    for i in range(len(tokens) - 1):
        bigrams[f"{tokens[i]} {tokens[i + 1]}"] += 1

    result: list[str] = []
    seen_phrases: set[str] = set()
    seen_words: set[str] = set()

    def _accept(phrase: str) -> bool:
        """Filter a candidate keyword: drop dupes, identical-token bigrams, stopword leaks."""
        lower = phrase.lower()
        if lower in seen_phrases:
            return False
        words = lower.split()
        # Bigrams of two identical tokens ("semantics semantics", "page page")
        # are heading-repetition noise that carries zero topical signal.
        if len(words) == 2 and words[0] == words[1]:
            return False
        # Defensive stopword check — token-level pruning already covers this,
        # but a phrase containing a known stopword shouldn't slip through any path.
        if any(w in _STOPWORDS or w in _URL_FRAGMENT_TOKENS for w in words):
            return False
        # Avoid keyword overlap with already-chosen phrases (was already in place).
        if any(w in seen_words for w in words):
            return False
        return True

    for phrase, count in bigrams.most_common(max_kw * 4):
        if count < 2:
            break
        if len(result) >= max_kw:
            break
        if not _accept(phrase):
            continue
        result.append(phrase)
        seen_phrases.add(phrase.lower())
        seen_words.update(phrase.lower().split())

    if len(result) < max_kw:
        for word, _ in unigrams.most_common(max_kw * 4):
            if len(result) >= max_kw:
                break
            if not _accept(word):
                continue
            result.append(word)
            seen_phrases.add(word.lower())
            seen_words.add(word)

    return result


def _input_phrase(source_kind: str) -> str:
    """Human-readable description of what a user typically hands you for this source kind."""
    return {
        _SOURCE_YOUTUBE: "a tutorial video URL",
        _SOURCE_WEB: "a single docs URL",
        _SOURCE_PDF: "a PDF / vendor whitepaper",
        _SOURCE_DOCS: "a docs site root URL",
    }.get(source_kind, "source material on this topic")


def _trigger_phrases(topic: str, source_kind: str) -> list[str]:
    """Three plausible user phrasings that should match this skill's description."""
    t = topic.strip() or "this topic"
    if source_kind == _SOURCE_YOUTUBE:
        return [f"explain {t}", f"how does {t} work", f"walk me through {t}"]
    if source_kind == _SOURCE_WEB:
        return [f"what is {t}", f"explain {t}", f"{t} reference"]
    if source_kind == _SOURCE_PDF:
        return [f"summarize this {t} document", f"what does the {t} doc say", f"explain {t}"]
    if source_kind == _SOURCE_DOCS:
        return [f"how do I use {t}", f"explain {t}", f"{t} docs"]
    return [f"explain {t}", f"what is {t}", f"how does {t} work"]


def _procedure_lead(topic: str, source_kind: str) -> str:
    """First sentence of the trigger description — what the skill DOES."""
    t = topic or "this topic"
    if source_kind == _SOURCE_YOUTUBE:
        return f"Apply lessons from a tutorial about {t}"
    if source_kind == _SOURCE_WEB:
        return f"Apply guidance from a docs page about {t}"
    if source_kind == _SOURCE_PDF:
        return f"Apply guidance from a PDF document about {t}"
    if source_kind == _SOURCE_DOCS:
        return f"Apply guidance from documentation about {t}"
    return f"Apply lessons about {t}"


def _generate_trigger_description(
    *, manifest: dict[str, Any], sections: list[dict[str, Any]]
) -> str:
    """Mechanically derive a triggerable SKILL.md ``description:`` from playbook content.

    Names the procedure, names 3–5 keyword chunks the skill covers, lists three
    plausible user phrasings that should match it, and the input type the user
    typically hands you. Trailing tag flags it as auto-generated.
    """
    source_kind = _source_kind(manifest)
    title = (manifest.get("video") or {}).get("title") or manifest.get("name") or ""
    topic = _topic_from_title(title)
    # Docs sites usually have generic page titles ("Getting started", "Overview"),
    # so prefer the first non-routing segment of the seed URL as the topic.
    if source_kind == _SOURCE_DOCS and topic.lower() in _GENERIC_DOC_TITLES:
        seed = (manifest.get("captureConfig") or {}).get("seedUrl") or manifest.get("sourceUrl") or ""
        url_topic = _topic_from_seed_url(seed)
        if url_topic:
            topic = url_topic
    keywords = _keywords(sections, source_kind)
    kw_str = ", ".join(keywords) if keywords else "concepts from the source"
    triggers = _trigger_phrases(topic, source_kind)
    trigger_str = ", ".join(f"'{t}'" for t in triggers)
    return (
        f"{_procedure_lead(topic, source_kind)}. Covers {kw_str}. "
        f"Use when the user says {trigger_str}, or hands you {_input_phrase(source_kind)}. "
        f"(auto-generated from {source_kind} source; refine via /codify for best matching)"
    )


def _source_block_lines(
    manifest: dict[str, Any], sections: list[dict[str, Any]]
) -> list[str]:
    """Render the 'Source playbook' bullet list, source-kind aware."""
    source_kind = _source_kind(manifest)
    name = manifest.get("name") or "?"
    source_url = manifest.get("sourceUrl") or "(unknown)"
    video = manifest.get("video") or {}
    title = video.get("title") or name
    config = manifest.get("captureConfig") or {}

    lines = [f"- **Playbook:** `{name}`"]

    if source_kind == _SOURCE_YOUTUBE:
        duration = video.get("durationSeconds")
        duration_label = (
            f"{int(duration) // 60} min" if isinstance(duration, (int, float)) else "?"
        )
        lines.append(f"- **Video:** {title} ({duration_label})")
        lines.append(f"- **Source URL:** {source_url}")
    elif source_kind == _SOURCE_WEB:
        # Word count isn't on the manifest; sum it off the distilled sections.
        word_count = sum(int(s.get("wordCount") or 0) for s in sections)
        word_label = f"{word_count} words" if word_count else "? words"
        lines.append(f"- **Source page:** {source_url} ({word_label})")
    elif source_kind == _SOURCE_PDF:
        page_count = config.get("pageCount")
        page_label = f"{page_count} pages" if page_count else "? pages"
        source_path = config.get("sourcePath") or ""
        filename = Path(source_path).name if source_path else title
        lines.append(f"- **Source PDF:** {filename} ({page_label})")
        lines.append(f"- **Source URL:** {source_url}")
    elif source_kind == _SOURCE_DOCS:
        pages_captured = config.get("pagesCaptured")
        pages_label = f"{pages_captured} pages" if pages_captured else "? pages"
        seed_url = config.get("seedUrl") or source_url
        lines.append(f"- **Source docs root:** {seed_url} ({pages_label})")
    else:
        lines.append(f"- **Source:** {title}")
        lines.append(f"- **Source URL:** {source_url}")

    return lines


def _section_label(section: dict[str, Any], source_kind: str) -> str:
    """Format the ``§N (...)`` lead of a section bullet, source-kind aware."""
    ordinal = section.get("ordinal", "?")
    if source_kind == _SOURCE_YOUTUBE:
        start = section.get("videoStartSeconds")
        end = section.get("videoEndSeconds")
        return f"§{ordinal} ({_mm_ss(start)}–{_mm_ss(end)})"
    if source_kind == _SOURCE_PDF:
        page = _pdf_page_number(section.get("text") or "", ordinal)
        return f"§{ordinal} (page {page})"
    # web + docs use the section heading inline.
    heading = _extract_doc_heading(section.get("text") or "")
    if heading:
        return f"§{ordinal} — {heading}"
    return f"§{ordinal}"


def _knows_preamble(source_kind: str, section_count: int) -> str:
    """Source-kind-aware lead-in sentence for the 'What this skill knows' block."""
    base = f"Distilled from {section_count} sections of the source playbook. "
    if source_kind == _SOURCE_YOUTUBE:
        return base + (
            "Each row below is one topical chunk of the tutorial with its video "
            "timestamp, so claims can be traced back to the source."
        )
    if source_kind == _SOURCE_PDF:
        return base + (
            "Each row below is one page of the source PDF, so claims can be "
            "traced back to the page."
        )
    if source_kind == _SOURCE_WEB:
        return base + (
            "Each row below is one heading-delimited section of the source page, "
            "so claims can be traced back to the heading."
        )
    if source_kind == _SOURCE_DOCS:
        return base + (
            "Each row below is one heading-delimited section across the crawled "
            "docs pages, so claims can be traced back to the heading."
        )
    return base + "Each row below is one section of the source."


def _render_scaffold_markdown(
    *,
    skill_name: str,
    playbook_meta: dict[str, Any],
    sections: list[dict[str, Any]],
    trigger_description: str,
    scope_notes: str | None,
) -> str:
    """Build the SKILL.md text from the playbook content and trigger metadata."""
    source_kind = _source_kind(playbook_meta)

    lines: list[str] = [
        "---",
        f"name: {skill_name}",
        f"description: {trigger_description}",
        "---",
        "",
        f"# {skill_name}",
        "",
        "## Source playbook",
        "",
    ]
    lines.extend(_source_block_lines(playbook_meta, sections))
    if playbook_meta.get("summary"):
        lines.append("")
        lines.append("**Playbook summary (author-supplied):**")
        lines.append("")
        lines.append(str(playbook_meta["summary"]))
    lines.append("")

    lines.append("## What this skill knows")
    lines.append("")
    lines.append(_knows_preamble(source_kind, len(sections)))
    lines.append("")
    for section in sections:
        snippet = _truncate_words((section.get("text") or "").strip(), 30)
        if not snippet:
            snippet = "_(no caption text)_"
        lines.append(f"- **{_section_label(section, source_kind)}** — {snippet}")
    lines.append("")

    lines.append("## How to apply")
    lines.append("")
    lines.append(
        "_(This section is a stub. Run `/codify " + skill_name +
        "` in a Claude Code session to have the current agent read the "
        "playbook's full lessons.md and turn the knowledge above into an "
        "actionable procedure: ordered steps, the tools to call, and "
        "verification checks. Edit by hand after that if you want.)_"
    )
    lines.append("")

    lines.append("## Source notes")
    lines.append("")
    lines.append(
        "- This skill carries the authority of one tutorial author. Cite the "
        "source section when applying claims so the user can trace them back."
    )
    if scope_notes:
        lines.append("- **Author scope notes:** " + scope_notes.strip())
    lines.append("")

    return "\n".join(lines)


def compose_skill_scaffold_from_playbook(
    playbook_name: str,
    skill_name: str,
    *,
    trigger_description: str | None = None,
    scope_notes: str | None = None,
    overwrite: bool = False,
    skills_root: str | None = None,
) -> dict[str, Any]:
    """Write a SKILL.md scaffold derived from a saved tutorial playbook.

    The scaffold lists every distilled section as a bullet (with timestamp,
    page number, or heading depending on source kind) and stubs out a
    "How to apply" block for the current Claude Code session to fill in
    via /codify. Returns the on-disk path and metadata about the composed
    scaffold.
    """
    skill_name = (skill_name or "").strip()
    if not skill_name:
        raise SkillSynthesisError("skill_name is required")
    target_dir = _playbook_dir(playbook_name)
    manifest_path = target_dir / "manifest.json"
    lessons_path = target_dir / "lessons.json"
    if not manifest_path.exists():
        raise SkillSynthesisError(
            f"playbook '{playbook_name}' not found; capture it first or check the name"
        )
    if not lessons_path.exists():
        raise SkillSynthesisError(
            f"playbook '{playbook_name}' has no lessons.json; "
            "run distill_tutorial_playbook first"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lessons = json.loads(lessons_path.read_text(encoding="utf-8"))
    sections = list(lessons.get("sections") or [])
    if not sections:
        raise SkillSynthesisError(
            f"playbook '{playbook_name}' lessons.json has no sections; "
            "re-distill the playbook"
        )

    if trigger_description is None:
        trigger_description = _generate_trigger_description(
            manifest=manifest, sections=sections
        )

    skill_slug = _slugify(skill_name)
    base_root = Path(skills_root) if skills_root else None
    target = _skill_dir(skill_slug, base=base_root)
    skill_md_path = target / "SKILL.md"

    with _lock:
        if skill_md_path.exists() and not overwrite:
            raise SkillSynthesisError(
                f"skill '{skill_name}' already exists at {skill_md_path}; "
                "pass overwrite=true to replace it"
            )
        target.mkdir(parents=True, exist_ok=True)
        scaffold = _render_scaffold_markdown(
            skill_name=skill_name,
            playbook_meta=manifest,
            sections=sections,
            trigger_description=trigger_description,
            scope_notes=scope_notes,
        )
        skill_md_path.write_text(scaffold, encoding="utf-8")

    # For web sources, surface the word count in the response so callers can
    # decide whether to include it elsewhere — the manifest doesn't store it
    # and re-deriving from sections is the only path.
    web_word_count = None
    if _source_kind(manifest) == _SOURCE_WEB:
        web_word_count = sum(int(s.get("wordCount") or 0) for s in sections)

    return {
        "ok": True,
        "skillName": skill_name,
        "skillSlug": skill_slug,
        "skillPath": str(skill_md_path),
        "skillDirectory": str(target),
        "sourcePlaybook": manifest.get("name"),
        "sourceKind": _source_kind(manifest),
        "sectionCount": len(sections),
        "triggerDescription": trigger_description,
        "webWordCount": web_word_count,
        "nextStep": (
            f"Run `/codify {skill_name}` in a Claude Code session to fill in "
            "the 'How to apply' section."
        ),
    }
