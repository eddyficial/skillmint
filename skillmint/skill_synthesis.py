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

# Shape constants. A playbook becomes one of:
#   - SKILL.md (one procedure, auto-loaded by Claude Code on trigger match)
#   - agent .md (a role that delegates to many existing skills)
#   - workflow .md (an orchestration document: sequenced skills with decision
#     gates, data flow, and rollback — owned by an agent, opt-in via explicit
#     shape="workflow"; no auto-detection because no reliable heuristic exists
#     for "is this curriculum prescribing an executable sequence?")
# The heuristic for agent fires on role-words in the title AND breadth
# (>= threshold sections), so a short "X in 100 Seconds" video doesn't get
# mis-classified.
_SHAPE_SKILL = "skill"
_SHAPE_AGENT = "agent"
_SHAPE_WORKFLOW = "workflow"
_AGENT_TITLE_PATTERN = re.compile(
    r"\b("
    r"bootcamp|masterclass|curriculum|roadmap|syllabus|"
    r"full\s+course|complete\s+course|complete\s+guide|"
    r"career|become\s+an?|from\s+zero\s+to|"
    r"step[\s-]by[\s-]step\s+(?:guide|tutorial)"
    r")\b",
    re.IGNORECASE,
)
_AGENT_SECTION_THRESHOLD = 10


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


def _agent_dir(*, base: Path | None = None) -> Path:
    """Return the directory where agent .md files live.

    Agents are single files at ``.claude/agents/<slug>.md`` (no per-agent
    subdirectory). ``base`` may be a project root or an existing ``.claude/*``
    path; the function normalizes either to the project's ``.claude/agents``.
    """
    root = base if base is not None else Path.cwd()
    parts = root.parts[-2:]
    if len(parts) == 2 and parts[-2] == ".claude" and parts[-1] in ("skills", "agents", "workflows"):
        return root.parent / "agents"
    return root / ".claude" / "agents"


def _workflow_dir(*, base: Path | None = None) -> Path:
    """Return the directory where workflow .md files live.

    Workflows are single files at ``.claude/workflows/<slug>.md``. Same
    normalization rules as ``_agent_dir``: caller can pass a project root or
    a sibling ``.claude/*`` directory.
    """
    root = base if base is not None else Path.cwd()
    parts = root.parts[-2:]
    if len(parts) == 2 and parts[-2] == ".claude" and parts[-1] in ("skills", "agents", "workflows"):
        return root.parent / "workflows"
    return root / ".claude" / "workflows"


def _detect_shape(manifest: dict[str, Any], sections: list[dict[str, Any]]) -> str:
    """Decide whether a playbook is shaped like a skill or an agent.

    Skill = one procedure, one trigger phrase, one ``SKILL.md``. Agent = a
    role that orchestrates many specialized skills (e.g. data-analyst spans
    SQL + Excel + Python + Power BI + Git). The signal is role-words in the
    source title AND breadth (section count), so a short "X in 100 Seconds"
    course doesn't get falsely promoted.
    """
    title = (manifest.get("video") or {}).get("title") or manifest.get("name") or ""
    has_role_words = bool(_AGENT_TITLE_PATTERN.search(title))
    enough_sections = len(sections) >= _AGENT_SECTION_THRESHOLD
    return _SHAPE_AGENT if (has_role_words and enough_sections) else _SHAPE_SKILL


def _mm_ss(seconds: float | int | None) -> str:
    """Render a seconds value as M:SS, or H:MM:SS when ≥ 1 hour.

    Long-form video (1h+ tutorials, full bootcamps) was rendering as e.g.
    ``565:51`` (565 minutes) instead of ``9:25:51``. Sections past hour 1
    became unreadable. Now compact for short clips, full H:MM:SS for long.
    """
    if seconds is None:
        return "?:??"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


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
    # Year prefix: "2026 X" -> "X" (only when leading the title so we don't
    # eat dates embedded mid-title).
    r"^\s*(?:19|20)\d{2}\s+",
    # Marketing decorations that show up in bootcamp / course titles.
    # ORDER MATTERS: "for FREE" must be stripped before standalone "FREE" or
    # "for" gets orphaned as a dangling preposition.
    r"\s*\bfor\s+FREE\b\s*",
    r"\s*\bFREE\b\s*",
    # Duration brackets: [24 Hours+], [24+ Hours], [24 hours]
    r"\s*\[\s*\d+\s*\+?\s*(?:Hours?|Minutes?)\+?\s*\]\s*",
    r"\s*\[\s*\d+\+\s*(?:Hours?|Minutes?)\s*\]\s*",
    # Role-shape suffixes — match in mid-title (followed by trailing prepositions
    # like "for", "and") as well as at end-of-title, so "Data Analyst Bootcamp for"
    # collapses to "Data Analyst".
    r"\s*\bBootcamp\b(?:\s+(?:for|and|with|to|in))?\s*$",
    r"\s*\bMasterclass\b(?:\s+(?:for|and|with|to|in))?\s*$",
    r"\s*\bRoadmap\b(?:\s+(?:for|and|with|to|in))?\s*$",
    r"\s*\bCurriculum\b(?:\s+(?:for|and|with|to|in))?\s*$",
    r"\s*\bSyllabus\b(?:\s+(?:for|and|with|to|in))?\s*$",
    # Dangling prepositions left over after marketing-word removal — applied
    # last so previous patterns don't have to know what came after their match.
    r"\s+(?:for|and|with|to|by|in)\s*$",
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
    # Title splits on common separators; pick the most distinctive chunk.
    for sep in (" | ", " — ", " - "):
        if sep in t:
            parts = [p.strip() for p in t.split(sep) if p.strip()]
            if parts:
                # Demote comma-heavy parts (3+ commas) — those are usually a
                # listing of tools/topics ("SQL, Excel, Python, Power BI, ...")
                # rather than the actual topic. Length is the tiebreaker.
                def _score(p: str) -> tuple[int, int]:
                    return (-1 if p.count(",") >= 3 else 0, len(p))
                t = max(parts, key=_score)
            break
    # Re-apply role-suffix patterns post-split: after picking the longest part,
    # a "Data Analyst Bootcamp" remainder needs its suffix trimmed.
    for pat in _TITLE_STRIP_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t or (title or "").strip()


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
    # Tutorial host catchphrases — repeat across every section, useless as topical signal.
    # ("What's going on everybody? Welcome back to another video. Today...")
    "everybody", "welcome", "today",
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


def _generate_agent_description(
    *, manifest: dict[str, Any], sections: list[dict[str, Any]]
) -> str:
    """Mechanically derive a triggerable ``description:`` for an agent scaffold.

    Agent descriptions name the role and the topics it orchestrates, then list
    role-shaped trigger phrases ("become a X", "I want to learn X end-to-end")
    instead of the procedure-shaped phrases a skill uses. The skill list is
    NOT hardcoded — the scaffold's `## Owned skills` section is the source of
    truth for what this agent delegates to (filled in by /codify).
    """
    source_kind = _source_kind(manifest)
    title = (manifest.get("video") or {}).get("title") or manifest.get("name") or ""
    topic = _topic_from_title(title)
    keywords = _keywords(sections, source_kind)
    kw_str = ", ".join(keywords) if keywords else "concepts from the source curriculum"
    triggers = [
        f"become a {topic}",
        f"learn {topic} end-to-end",
        f"build a {topic} portfolio",
        f"what does a {topic} do",
    ]
    trigger_str = ", ".join(f"'{t}'" for t in triggers[:3])
    return (
        f"Orchestrating agent for end-to-end {topic} work. Covers {kw_str}. "
        f"Use when the user says {trigger_str}, or hands you a multi-skill {topic} task. "
        f"Delegates to the skills listed in `## Owned skills` (filled in by /codify). "
        f"(auto-generated from {source_kind} source; refine via /codify for best matching)"
    )


def _generate_workflow_description(
    *, manifest: dict[str, Any], sections: list[dict[str, Any]]
) -> str:
    """Mechanically derive a triggerable ``description:`` for a workflow scaffold.

    Workflow descriptions name the sequenced procedure and the inputs/outputs,
    framing it as an executable orchestration, not a curriculum or skill.
    """
    source_kind = _source_kind(manifest)
    title = (manifest.get("video") or {}).get("title") or manifest.get("name") or ""
    topic = _topic_from_title(title)
    keywords = _keywords(sections, source_kind)
    kw_str = ", ".join(keywords) if keywords else "the source curriculum"
    return (
        f"Orchestration workflow for {topic}. Sequences skills with decision "
        f"gates, data flow, and rollback. Covers {kw_str}. Use when the user "
        f"says 'run the {topic} workflow', 'walk through {topic} end-to-end', "
        f"or hands you a multi-step {topic} task that needs deterministic "
        f"ordering. (auto-generated from {source_kind} source; refine via "
        "/codify for best matching)"
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


def _visual_action_summary(section: dict[str, Any], *, limit: int = 2) -> str:
    actions = section.get("visualActions") or []
    if not actions:
        return ""
    chunks: list[str] = []
    for action in actions[:limit]:
        action_type = str(action.get("actionType") or "unknown").replace("_", " ")
        detail = action.get("visibleTextSample") or "; ".join(action.get("observations") or [])
        if detail:
            detail = _truncate_words(str(detail), 10)
            chunks.append(f"{action_type}: {detail}")
        else:
            chunks.append(action_type)
    if len(actions) > limit:
        chunks.append(f"+{len(actions) - limit} more")
    return "; ".join(chunks)


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
    """Build the SKILL.md text from the playbook content and trigger metadata.

    Output includes the YAML frontmatter + governance sections (typed Inputs,
    Outputs, Success criteria, Failure modes, Dependencies) as /codify stubs.
    The compose step writes the *structure*; /codify fills in the procedure
    + typed contracts from the playbook's lessons.md.
    """
    source_kind = _source_kind(playbook_meta)

    lines: list[str] = [
        "---",
        f"name: {skill_name}",
        f"description: {trigger_description}",
        # Typed-contract keys. compose ships them as null placeholders so the
        # frontmatter schema is stable; /codify replaces them with the actual
        # input/output types from the procedure.
        "inputs: null  # filled by /codify: {arg_name: type, ...}",
        "outputs: null  # filled by /codify: {artifact: path | none, ...}",
        "dependencies: null  # filled by /codify: [mcp tools, sibling skills, env vars]",
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
        visual = _visual_action_summary(section)
        lines.append(f"- **{_section_label(section, source_kind)}** — {snippet}")
        if visual:
            lines.append(f"  - Visual: {visual}")
    lines.append("")

    lines.append("## Inputs")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {skill_name}` declares the typed inputs this skill "
        "expects from the calling Claude session — argument names, types, "
        "and whether each is required. Example schema: `user_prompt: string "
        "(required)`, `target_path: pathlib.Path (optional)`.)_"
    )
    lines.append("")

    lines.append("## Outputs")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {skill_name}` declares the typed outputs this "
        "skill produces — artifacts written to disk, return values, conversation "
        "state. Example schema: `artifact: pathlib.Path | None`, "
        "`status: 'completed' | 'partial' | 'failed'`.)_"
    )
    lines.append("")

    lines.append("## How to apply")
    lines.append("")
    lines.append(
        f"_(Stub. Run `/codify {skill_name}` in a Claude Code session to have "
        "the current agent read the playbook's full lessons.md and turn the "
        "knowledge above into an actionable procedure: ordered steps, the "
        "tools to call, and verification checks. Edit by hand after that.)_"
    )
    lines.append("")

    lines.append("## Success criteria")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {skill_name}` declares concrete completion tests — "
        "the skill is *done* when X file exists / Y command exits 0 / Z "
        "assertion holds. Without these, callers cannot tell success from "
        "silent failure.)_"
    )
    lines.append("")

    lines.append("## Failure modes")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {skill_name}` enumerates the cases this skill "
        "explicitly does NOT handle — the negative-space contract that "
        "complements the trigger description. Examples: 'does not handle "
        "live videos', 'requires ffmpeg on PATH', 'rejects PDFs with no "
        "extractable text'.)_"
    )
    lines.append("")

    lines.append("## Dependencies")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {skill_name}` declares what this skill needs to "
        "run — MCP servers, sibling skills it invokes, environment variables, "
        "filesystem paths, network reachability. Cross-references should be "
        "explicit so a stale dependency surfaces as a missing-tool error, "
        "not as a silent skip.)_"
    )
    lines.append("")

    lines.append("## Source notes")
    lines.append("")
    lines.append(
        "- This skill carries the authority of one tutorial author. Cite the "
        "source section when applying claims so the user can trace them back."
    )
    lines.append(
        "- **Codify dependency:** the `## Inputs`, `## Outputs`, `## How to "
        "apply`, `## Success criteria`, `## Failure modes`, and `## Dependencies` "
        "sections above are placeholders until `/codify` runs. `/codify` is "
        "provided by the Periphery MCP server, not Skillmint itself."
    )
    if scope_notes:
        lines.append("- **Author scope notes:** " + scope_notes.strip())
    lines.append("")

    return "\n".join(lines)


def _render_agent_scaffold_markdown(
    *,
    agent_name: str,
    playbook_meta: dict[str, Any],
    sections: list[dict[str, Any]],
    trigger_description: str,
    scope_notes: str | None,
) -> str:
    """Build the agent .md text from a curriculum-shaped playbook.

    Differs from the skill scaffold: agents orchestrate, they don't do.
    Sections become a curriculum (what the agent walks the user through),
    plus governance sections (Inputs / Outputs / Owned skills / Constraints
    / Error handling) that /codify fills in from the source lessons.
    """
    source_kind = _source_kind(playbook_meta)

    lines: list[str] = [
        "---",
        f"name: {agent_name}",
        f"description: {trigger_description}",
        # Typed-contract keys for the agent's external surface. /codify
        # replaces these placeholders with the actual delegation contract.
        "inputs: null  # filled by /codify: {arg_name: type, ...} the user task shape",
        "outputs: null  # filled by /codify: {artifact: path | none, ...} the delivered result",
        "owned_skills: null  # filled by /codify: [skill_name, ...] enforced delegation set",
        "---",
        "",
        f"# {agent_name}",
        "",
        "## Role",
        "",
        (
            "This is an **orchestrating agent**, not a single-procedure skill. "
            "It picks the right specialized skill for each phase of a larger "
            "task and cites the source curriculum for sequencing and "
            "dependencies."
        ),
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

    lines.append("## Curriculum")
    lines.append("")
    lines.append(
        f"Distilled from {len(sections)} sections of the source playbook. "
        "Each row is one topical chunk of the curriculum with its source "
        "location, so the agent can cite specific lessons when picking "
        "what to teach or do next."
    )
    lines.append("")
    for section in sections:
        snippet = _truncate_words((section.get("text") or "").strip(), 30)
        if not snippet:
            snippet = "_(no caption text)_"
        lines.append(f"- **{_section_label(section, source_kind)}** — {snippet}")
    lines.append("")

    lines.append("## Inputs")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {agent_name}` declares the typed inputs this agent "
        "accepts — what the user (or another agent) hands over when delegating. "
        "Example: `user_goal: string (required)`, `target_artifact: pathlib.Path "
        "(optional)`, `deadline: datetime (optional)`.)_"
    )
    lines.append("")

    lines.append("## Outputs")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {agent_name}` declares the typed outputs this agent "
        "produces when the delegated task is complete — final artifacts, "
        "summary state, hand-back signals. Example: `deliverable: pathlib.Path`, "
        "`status: 'completed' | 'partial' | 'failed'`, `next_action: string | None`.)_"
    )
    lines.append("")

    lines.append("## Owned skills")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {agent_name}` reads the playbook's lessons.md, "
        "identifies which `.claude/skills/*` skills map to which curriculum "
        "sections, and writes the orchestration table below. Required schema:)_"
    )
    lines.append("")
    lines.append("| Skill | Curriculum section(s) | When to delegate | Input handoff | Output expected |")
    lines.append("|---|---|---|---|---|")
    lines.append("| _(stub)_ | _(stub)_ | _(stub)_ | _(stub)_ | _(stub)_ |")
    lines.append("")

    lines.append("## When to invoke this agent")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {agent_name}` replaces this with concrete trigger "
        "phrases — when to delegate to this agent vs. call a tactical skill "
        "directly, what inputs to expect, and which user phrasings should "
        "route here.)_"
    )
    lines.append("")

    lines.append("## Constraints")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {agent_name}` declares the explicit scope of "
        "authority — what this agent will NOT do without user confirmation "
        "(e.g., 'does not push to git', 'does not execute SQL against "
        "production', 'does not send external messages'), and what it must "
        "defer to a human for.)_"
    )
    lines.append("")

    lines.append("## Error handling")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {agent_name}` declares what this agent does when "
        "an owned skill fails, returns ambiguous output, or is missing from "
        "`.claude/skills/`. Required cases: (a) skill not found, (b) skill "
        "returned failure status, (c) skill produced an output the next step "
        "cannot consume, (d) user input is ambiguous between two skills.)_"
    )
    lines.append("")

    lines.append("## Source notes")
    lines.append("")
    lines.append(
        "- This agent represents one curriculum author's view of the role. "
        "Cite the source section when picking what to teach or do next."
    )
    lines.append(
        "- **Codify dependency:** the `## Inputs`, `## Outputs`, `## Owned "
        "skills`, `## When to invoke this agent`, `## Constraints`, and "
        "`## Error handling` sections above are placeholders until `/codify` "
        "runs. `/codify` is provided by the Periphery MCP server, not "
        "Skillmint itself."
    )
    if scope_notes:
        lines.append("- **Author scope notes:** " + scope_notes.strip())
    lines.append("")

    return "\n".join(lines)


def _render_workflow_scaffold_markdown(
    *,
    workflow_name: str,
    playbook_meta: dict[str, Any],
    sections: list[dict[str, Any]],
    trigger_description: str,
    scope_notes: str | None,
    owner_agent: str | None,
) -> str:
    """Build the workflow .md text — an orchestration document.

    Differs from skill (one procedure) and agent (role + delegation): workflows
    sequence skills with explicit decision gates, declared data flow between
    steps, and rollback handlers. They are owned by an agent and reference the
    skills the agent delegates to.
    """
    source_kind = _source_kind(playbook_meta)

    lines: list[str] = [
        "---",
        f"name: {workflow_name}",
        f"description: {trigger_description}",
        # Typed contract: workflows declare inputs and outputs at the boundary
        # of the orchestration (what the caller hands in / gets back), plus an
        # explicit owner_agent so dispatch knows who is accountable.
        "inputs: null  # filled by /codify: {arg_name: type, ...}",
        "outputs: null  # filled by /codify: {artifact: path | none, ...}",
        f"owner_agent: {owner_agent or 'null'}  # the agent that runs this workflow",
        "rollback_strategy: null  # filled by /codify: per-step or whole-workflow reversal",
        "---",
        "",
        f"# {workflow_name}",
        "",
        "## Role",
        "",
        (
            "This is an **orchestration workflow**, not a skill or an agent. "
            "It encodes a deterministic sequence of skill invocations with "
            "decision gates, declared data flow, and rollback semantics. The "
            "owning agent dispatches this workflow when the user's request "
            "matches the trigger description."
        ),
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

    lines.append("## Inputs")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {workflow_name}` declares the typed inputs this "
        "workflow accepts at the entry boundary — what the calling agent "
        "must supply before the first step runs.)_"
    )
    lines.append("")

    lines.append("## Outputs")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {workflow_name}` declares the typed outputs this "
        "workflow produces at the exit boundary — what the calling agent "
        "receives after the last step completes successfully.)_"
    )
    lines.append("")

    lines.append("## Steps")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {workflow_name}` reads the playbook's lessons.md, "
        "translates the curriculum into an ordered sequence of skill "
        "invocations, and writes the table below. Required schema:)_"
    )
    lines.append("")
    lines.append("| # | Skill | Input (from) | Output (to) | Success gate | On failure |")
    lines.append("|---|---|---|---|---|---|")
    lines.append("| 1 | _(stub)_ | _(input source)_ | _(next step / output)_ | _(assertion)_ | _(retry / rollback / abort)_ |")
    lines.append("")

    lines.append("## Decision gates")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {workflow_name}` enumerates branch points where the "
        "next step depends on a prior step's output — e.g., 'if `validation.status` "
        "== \"passed\" → step 4, else → rollback'. No conditional logic should "
        "live implicitly inside step descriptions; gates must be explicit.)_"
    )
    lines.append("")

    lines.append("## Data flow")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {workflow_name}` documents how data moves between "
        "steps — what artifact each step produces, what the next step expects, "
        "and where the workflow's inputs/outputs enter and exit. Format: "
        "`step_N.output_key → step_M.input_key`.)_"
    )
    lines.append("")

    lines.append("## Rollback")
    lines.append("")
    lines.append(
        f"_(Stub. `/codify {workflow_name}` defines the reversal procedure for "
        "each side-effecting step — what to undo, in what order, and the "
        "termination condition. Workflows without rollback semantics are "
        "explicitly marked `rollback_strategy: not_applicable` (read-only) "
        "or `rollback_strategy: manual` (operator must repair).)_"
    )
    lines.append("")

    lines.append("## Curriculum reference")
    lines.append("")
    lines.append(
        f"Distilled from {len(sections)} sections of the source playbook. "
        "Steps above should cite these sections so each invocation is traceable."
    )
    lines.append("")
    for section in sections:
        snippet = _truncate_words((section.get("text") or "").strip(), 30)
        if not snippet:
            snippet = "_(no caption text)_"
        lines.append(f"- **{_section_label(section, source_kind)}** — {snippet}")
    lines.append("")

    lines.append("## Source notes")
    lines.append("")
    lines.append(
        "- This workflow reflects one curriculum author's view of the sequence. "
        "Cite the source section for each step so the ordering is auditable."
    )
    lines.append(
        "- **Codify dependency:** the `## Inputs`, `## Outputs`, `## Steps`, "
        "`## Decision gates`, `## Data flow`, and `## Rollback` sections above "
        "are placeholders until `/codify` runs. `/codify` is provided by the "
        "Periphery MCP server, not Skillmint itself."
    )
    lines.append(
        "- **Owner agent:** workflows do not stand alone; the agent named in "
        "the `owner_agent` frontmatter key is the dispatch entry point. If "
        "`owner_agent: null`, set it before shipping or the workflow is "
        "unreachable from the agent layer."
    )
    if scope_notes:
        lines.append("- **Author scope notes:** " + scope_notes.strip())
    lines.append("")

    return "\n".join(lines)


def compose_skill_scaffold_from_playbook(
    playbook_name: str,
    skill_name: str,
    *,
    shape: str = "auto",
    trigger_description: str | None = None,
    scope_notes: str | None = None,
    owner_agent: str | None = None,
    overwrite: bool = False,
    skills_root: str | None = None,
) -> dict[str, Any]:
    """Write a SKILL.md, agent .md, or workflow .md scaffold from a saved playbook.

    ``shape`` is ``"auto"`` (heuristic on title role-words + section count;
    selects skill or agent), ``"skill"`` (force ``.claude/skills/<slug>/SKILL.md``),
    ``"agent"`` (force ``.claude/agents/<slug>.md``), or ``"workflow"`` (force
    ``.claude/workflows/<slug>.md`` — orchestration document with sequenced
    steps, decision gates, data flow, and rollback). Workflow shape is opt-in
    only; the heuristic never selects it because no reliable signal exists for
    "this curriculum prescribes an executable sequence."

    ``owner_agent`` is recorded in the workflow's frontmatter (and ignored for
    other shapes); workflows do not stand alone — an agent must dispatch them.

    The scaffold lists every distilled section as a bullet (with timestamp,
    page number, or heading depending on source kind) and stubs out a codify
    block for the current Claude Code session to fill in via /codify. Returns
    the on-disk path and metadata about the composed scaffold.
    """
    skill_name = (skill_name or "").strip()
    if not skill_name:
        raise SkillSynthesisError("skill_name is required")
    if shape not in ("auto", _SHAPE_SKILL, _SHAPE_AGENT, _SHAPE_WORKFLOW):
        raise SkillSynthesisError(
            f"invalid shape={shape!r}; expected 'auto', 'skill', 'agent', or 'workflow'"
        )
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

    resolved_shape = shape if shape != "auto" else _detect_shape(manifest, sections)
    is_agent = resolved_shape == _SHAPE_AGENT
    is_workflow = resolved_shape == _SHAPE_WORKFLOW

    if trigger_description is None:
        if is_workflow:
            trigger_description = _generate_workflow_description(
                manifest=manifest, sections=sections
            )
        elif is_agent:
            trigger_description = _generate_agent_description(
                manifest=manifest, sections=sections
            )
        else:
            trigger_description = _generate_trigger_description(
                manifest=manifest, sections=sections
            )

    slug = _slugify(skill_name)
    base_root = Path(skills_root) if skills_root else None

    if is_workflow:
        workflows_root = _workflow_dir(base=base_root)
        output_path = workflows_root / f"{slug}.md"
        output_dir = workflows_root
    elif is_agent:
        agents_root = _agent_dir(base=base_root)
        output_path = agents_root / f"{slug}.md"
        output_dir = agents_root
    else:
        output_dir = _skill_dir(slug, base=base_root)
        output_path = output_dir / "SKILL.md"

    with _lock:
        if output_path.exists() and not overwrite:
            kind_word = resolved_shape
            raise SkillSynthesisError(
                f"{kind_word} '{skill_name}' already exists at {output_path}; "
                "pass overwrite=true to replace it"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        if is_workflow:
            scaffold = _render_workflow_scaffold_markdown(
                workflow_name=skill_name,
                playbook_meta=manifest,
                sections=sections,
                trigger_description=trigger_description,
                scope_notes=scope_notes,
                owner_agent=owner_agent,
            )
        elif is_agent:
            scaffold = _render_agent_scaffold_markdown(
                agent_name=skill_name,
                playbook_meta=manifest,
                sections=sections,
                trigger_description=trigger_description,
                scope_notes=scope_notes,
            )
        else:
            scaffold = _render_scaffold_markdown(
                skill_name=skill_name,
                playbook_meta=manifest,
                sections=sections,
                trigger_description=trigger_description,
                scope_notes=scope_notes,
            )
        output_path.write_text(scaffold, encoding="utf-8")

    # For web sources, surface the word count in the response so callers can
    # decide whether to include it elsewhere — the manifest doesn't store it
    # and re-deriving from sections is the only path.
    web_word_count = None
    if _source_kind(manifest) == _SOURCE_WEB:
        web_word_count = sum(int(s.get("wordCount") or 0) for s in sections)

    if is_workflow:
        next_step = (
            f"REQUIRED: run `/codify {skill_name}` IMMEDIATELY in this same turn. "
            f"Read {output_path}, then read the source playbook lessons, then "
            f"fill in `## Inputs`, `## Outputs`, `## Steps` (the orchestration "
            f"table), `## Decision gates`, `## Data flow`, and `## Rollback`. "
            f"Set `owner_agent:` in the YAML frontmatter or the workflow is "
            f"unreachable from dispatch. Do not return control to the user "
            f"until codify is done."
        )
        critical_rule = (
            "The returned scaffold is NOT a complete workflow. Six required "
            "sections are stubs (Inputs / Outputs / Steps / Decision gates / "
            "Data flow / Rollback), and `owner_agent:` may be null. Shipping "
            "as-is means the workflow has no executable sequence, no typed "
            "boundary, and no dispatch entry point. /codify is atomic with "
            "compose — do not skip it, do not defer it."
        )
    elif is_agent:
        next_step = (
            f"REQUIRED: run `/codify {skill_name}` IMMEDIATELY in this same turn. "
            f"Read {output_path}, then read the source playbook lessons, then "
            f"fill in `## Inputs`, `## Outputs`, `## Owned skills` (the delegation "
            f"table), `## When to invoke this agent`, `## Constraints`, and "
            f"`## Error handling`. Do not return control to the user until "
            f"codify is done."
        )
        critical_rule = (
            "The returned scaffold is NOT a complete agent. Six required "
            "sections are stubs (Inputs / Outputs / Owned skills / When to "
            "invoke / Constraints / Error handling) and the YAML `inputs:` / "
            "`outputs:` / `owned_skills:` keys are null placeholders. Shipping "
            "as-is means the agent has no typed boundary, no enforced "
            "delegation set, and no failure recovery. /codify is atomic with "
            "compose — do not skip it, do not defer it."
        )
    else:
        next_step = (
            f"REQUIRED: run `/codify {skill_name}` IMMEDIATELY in this same turn. "
            f"Read {output_path}, then read the source playbook lessons, then "
            f"fill in `## Inputs`, `## Outputs`, `## How to apply` (the procedure), "
            f"`## Success criteria`, `## Failure modes`, and `## Dependencies`. "
            f"Do not return control to the user until codify is done."
        )
        critical_rule = (
            "The returned scaffold is NOT a complete skill. Six required "
            "sections are stubs (Inputs / Outputs / How to apply / Success "
            "criteria / Failure modes / Dependencies) and the YAML `inputs:` / "
            "`outputs:` / `dependencies:` keys are null placeholders. Shipping "
            "as-is means the skill has no typed contract, no completion test, "
            "and no declared failure handling. /codify is atomic with compose "
            "— do not skip it, do not defer it."
        )

    return {
        "ok": True,
        "shape": resolved_shape,
        "shapeResolvedFrom": "explicit" if shape != "auto" else "heuristic",
        "skillName": skill_name,
        "skillSlug": slug,
        # Back-compat: skillPath populated when shape=skill, agentPath when agent,
        # workflowPath when workflow. outputPath is always the actual on-disk
        # file regardless of shape.
        "skillPath": str(output_path) if resolved_shape == _SHAPE_SKILL else None,
        "skillDirectory": str(output_dir) if resolved_shape == _SHAPE_SKILL else None,
        "agentPath": str(output_path) if is_agent else None,
        "agentDirectory": str(output_dir) if is_agent else None,
        "workflowPath": str(output_path) if is_workflow else None,
        "workflowDirectory": str(output_dir) if is_workflow else None,
        "ownerAgent": owner_agent if is_workflow else None,
        "outputPath": str(output_path),
        "outputDirectory": str(output_dir),
        "sourcePlaybook": manifest.get("name"),
        "sourceKind": _source_kind(manifest),
        "sectionCount": len(sections),
        "triggerDescription": trigger_description,
        "webWordCount": web_word_count,
        # nextStep is a hard contract — every successful compose call must be
        # immediately followed by /codify in the same turn. The scaffold is
        # NOT a complete skill/agent/workflow; the auto-generated description
        # is a default, the codify stubs are literal placeholders, and shipping
        # any of them as-is is a known-bad outcome.
        "nextStep": next_step,
        "criticalRule": critical_rule,
    }
