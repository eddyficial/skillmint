"""Tests for skillmint.skill_synthesis (scaffold composition from playbooks).

The tests fully mock the playbook on disk so they don't depend on a captured tutorial.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillmint import skill_synthesis as ss


def _write_playbook(
    tmp_path: Path,
    *,
    name: str = "fake-tutorial",
    sections_text: list[str] | None = None,
    summary: str | None = None,
) -> Path:
    """Write a minimal video-shape manifest.json + lessons.json under tmp_path/<name>/."""
    playbook_dir = tmp_path / name
    playbook_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "slug": name,
        "sourceUrl": f"https://youtu.be/{name}",
        "video": {"title": f"Fake video {name}", "channel": "tester", "durationSeconds": 300},
        "summary": summary,
    }
    sections_text = sections_text or [
        "First section content about general topic.",
        "Second section content about another general topic.",
    ]
    sections = [
        {
            "ordinal": i + 1,
            "videoStartSeconds": i * 60.0,
            "videoEndSeconds": (i + 1) * 60.0,
            "text": text,
            "anchorKeyframePath": f"keyframes/{i+1:03d}.jpg",
        }
        for i, text in enumerate(sections_text)
    ]
    lessons = {
        "name": name,
        "sourceUrl": manifest["sourceUrl"],
        "video": manifest["video"],
        "sectionCount": len(sections),
        "sections": sections,
    }
    (playbook_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (playbook_dir / "lessons.json").write_text(json.dumps(lessons), encoding="utf-8")
    return playbook_dir


def _write_document_playbook(
    tmp_path: Path,
    *,
    name: str,
    source_kind: str,
    title: str,
    sections_text: list[str],
    capture_config_extras: dict | None = None,
    section_word_counts: list[int] | None = None,
    source_url: str | None = None,
) -> Path:
    """Write a manifest.json + lessons.json shaped like a document capture (web/pdf/docs)."""
    playbook_dir = tmp_path / name
    playbook_dir.mkdir(parents=True, exist_ok=True)
    config: dict = {"sourceKind": source_kind}
    if capture_config_extras:
        config.update(capture_config_extras)
    manifest = {
        "name": name,
        "slug": name,
        "sourceUrl": source_url or f"https://example.com/{name}",
        "video": {"title": title, "channel": "example.com"},
        "captureConfig": config,
        "summary": None,
    }
    sections = []
    for i, text in enumerate(sections_text):
        section = {
            "ordinal": i + 1,
            "startedAt": None,
            "videoStartSeconds": None,
            "videoEndSeconds": None,
            "trigger": "document_section",
            "anchorKeyframePath": None,
            "stepOrdinals": [i + 1],
            "text": text,
        }
        if section_word_counts and i < len(section_word_counts):
            section["wordCount"] = section_word_counts[i]
        sections.append(section)
    lessons = {
        "name": name,
        "sourceUrl": manifest["sourceUrl"],
        "video": manifest["video"],
        "sectionCount": len(sections),
        "sections": sections,
    }
    (playbook_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (playbook_dir / "lessons.json").write_text(json.dumps(lessons), encoding="utf-8")
    return playbook_dir


def test_compose_scaffold_writes_skill_md(tmp_path, monkeypatch) -> None:
    """Happy path: scaffold lands at .claude/skills/<slug>/SKILL.md with all expected blocks."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="happy-path")
    result = ss.compose_skill_scaffold_from_playbook(
        "happy-path",
        skill_name="happy skill",
        skills_root=str(tmp_path / "project"),
    )
    assert result["ok"] is True
    assert result["skillSlug"] == "happy-skill"
    skill_md = Path(result["skillPath"])
    assert skill_md.exists()
    body = skill_md.read_text(encoding="utf-8")
    assert body.startswith("---\nname: happy skill\n")
    assert "## Source playbook" in body
    assert "## What this skill knows" in body
    assert "## How to apply" in body
    assert "/codify happy skill" in body
    assert "## Source notes" in body


def test_compose_response_mandates_codify_as_next_step(tmp_path, monkeypatch) -> None:
    """Every compose response must emit a hard `nextStep` + `criticalRule` telling the caller
    to run /codify IMMEDIATELY in the same turn — scaffolds shipped as-is have lazy auto-generated
    descriptions + stub `How to apply` blocks and are functionally useless until codified.
    """
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="codify-contract")
    result = ss.compose_skill_scaffold_from_playbook(
        "codify-contract",
        skill_name="codify-contract-skill",
        skills_root=str(tmp_path / "project"),
    )
    # nextStep must explicitly mention codify, the skill name, and "immediately"-class urgency.
    assert "nextStep" in result
    step = result["nextStep"]
    assert "/codify" in step
    assert "codify-contract-skill" in step
    assert "IMMEDIATELY" in step or "immediately" in step.lower()
    # criticalRule must exist and convey that the scaffold is NOT a complete skill.
    assert "criticalRule" in result
    rule = result["criticalRule"]
    assert "NOT a complete skill" in rule or "not a complete skill" in rule.lower()
    assert "stub" in rule.lower()


def test_compose_scaffold_default_trigger_uses_video_title(tmp_path, monkeypatch) -> None:
    """When trigger_description is omitted, the default references the source video title."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="trigger-default")
    result = ss.compose_skill_scaffold_from_playbook(
        "trigger-default",
        skill_name="auto-trigger",
        skills_root=str(tmp_path / "project"),
    )
    assert "Fake video trigger-default" in result["triggerDescription"]


def test_compose_scaffold_refuses_to_overwrite_without_flag(tmp_path, monkeypatch) -> None:
    """A second compose call against the same skill name fails unless overwrite=True."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="pb")
    ss.compose_skill_scaffold_from_playbook(
        "pb", skill_name="taken", skills_root=str(tmp_path / "project")
    )
    with pytest.raises(ss.SkillSynthesisError, match="already exists"):
        ss.compose_skill_scaffold_from_playbook(
            "pb", skill_name="taken", skills_root=str(tmp_path / "project")
        )
    # With overwrite=True it succeeds.
    result = ss.compose_skill_scaffold_from_playbook(
        "pb", skill_name="taken", skills_root=str(tmp_path / "project"), overwrite=True
    )
    assert result["ok"] is True


def test_compose_scaffold_rejects_missing_playbook(tmp_path, monkeypatch) -> None:
    """A non-existent playbook surfaces a SkillSynthesisError."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    with pytest.raises(ss.SkillSynthesisError, match="not found"):
        ss.compose_skill_scaffold_from_playbook(
            "does-not-exist", skill_name="x", skills_root=str(tmp_path / "project")
        )


def test_compose_scaffold_skills_root_accepts_dotclaude_suffix(tmp_path, monkeypatch) -> None:
    """skills_root accepts both a project root AND a path already ending in .claude/skills.

    Both forms must produce the same final SKILL.md path. Without this, callers who pass
    the literal skills directory get a nested .claude/skills/.claude/skills/<slug>/ path.
    """
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="suffix-test")
    project_root = tmp_path / "project"
    skills_dir = project_root / ".claude" / "skills"

    # Form 1: project root
    r1 = ss.compose_skill_scaffold_from_playbook(
        "suffix-test", skill_name="form-one", skills_root=str(project_root)
    )
    # Form 2: .claude/skills path directly
    r2 = ss.compose_skill_scaffold_from_playbook(
        "suffix-test", skill_name="form-two", skills_root=str(skills_dir)
    )

    assert Path(r1["skillPath"]) == skills_dir / "form-one" / "SKILL.md"
    assert Path(r2["skillPath"]) == skills_dir / "form-two" / "SKILL.md"
    # Critically: no nested .claude/skills/.claude/skills/ in either path.
    assert ".claude\\skills\\.claude\\skills" not in r2["skillPath"]
    assert ".claude/skills/.claude/skills" not in r2["skillPath"]


def test_compose_scaffold_rejects_undistilled_playbook(tmp_path, monkeypatch) -> None:
    """If lessons.json is missing the caller is told to distill first."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    pb = tmp_path / "raw"
    pb.mkdir()
    (pb / "manifest.json").write_text(json.dumps({"name": "raw"}), encoding="utf-8")
    with pytest.raises(ss.SkillSynthesisError, match="distill"):
        ss.compose_skill_scaffold_from_playbook(
            "raw", skill_name="x", skills_root=str(tmp_path / "project")
        )


def test_video_trigger_description_names_triggers_and_input_type(tmp_path, monkeypatch) -> None:
    """YouTube playbooks get a description naming user-phrasing triggers + 'tutorial video URL'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(
        tmp_path,
        name="docker-intro",
        sections_text=[
            "Docker is a containerization platform that packages software with all dependencies.",
            "Containers run reliably across environments, from laptops to cloud servers.",
            "Build images from a Dockerfile; images are immutable snapshots of your software.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "docker-intro", skill_name="docker-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    assert "tutorial" in desc.lower()
    assert "a tutorial video URL" in desc
    assert "auto-generated from youtube_video source" in desc
    assert "whenever the user asks" in desc
    assert desc.count("'") >= 6  # at least three single-quoted trigger phrases


def test_web_trigger_description_names_triggers_and_input_type(tmp_path, monkeypatch) -> None:
    """Web-page playbooks get triggers like 'what is X', 'X reference' + 'single docs URL'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="http-methods",
        source_kind="web_page",
        title="HTTP request methods - HTTP | MDN",
        source_url="https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods",
        sections_text=[
            "HTTP request methods / https://example.com/ HTTP defines request methods like GET, POST, PUT, DELETE for resource manipulation.",
            "GET / https://example.com/ The GET method requests a representation of the resource. GET requests should only retrieve data.",
            "POST / https://example.com/ The POST method submits an entity to the resource, often causing a state change.",
        ],
        section_word_counts=[20, 18, 17],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "http-methods", skill_name="http-methods-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    assert "a single docs URL" in desc
    assert "auto-generated from web_page source" in desc
    assert "what is" in desc.lower()
    assert "reference" in desc.lower()


def test_pdf_trigger_description_names_triggers_and_input_type(tmp_path, monkeypatch) -> None:
    """PDF playbooks get triggers like 'summarize this X document' + 'PDF / vendor whitepaper'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="risk-disclosure",
        source_kind="pdf",
        title="Sample Risk Disclosure Addendum",
        source_url="file:///C:/path/to/risk.pdf",
        capture_config_extras={
            "sourcePath": "C:\\path\\to\\risk.pdf",
            "pageCount": 9,
        },
        sections_text=[
            "Page 1 / page 1 / file:// Risk disclosure futures trading commodity markets.",
            "Page 2 / page 2 / file:// Customer funds protection futures commission merchant requirements.",
            "Page 3 / page 3 / file:// Trading risk substantial loss possible deposit margin requirements.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "risk-disclosure", skill_name="risk-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    assert "PDF / vendor whitepaper" in desc
    assert "auto-generated from pdf source" in desc
    assert "summarize" in desc.lower()
    assert "document" in desc.lower()


def test_docs_trigger_description_names_triggers_and_input_type(tmp_path, monkeypatch) -> None:
    """Docs-site playbooks get triggers like 'how do I use X', 'X docs' + 'docs site root URL'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="uv-docs",
        source_kind="documentation_site",
        title="Getting started | uv",
        source_url="https://docs.astral.sh/uv/getting-started/",
        capture_config_extras={
            "seedUrl": "https://docs.astral.sh/uv/getting-started/",
            "pagesCaptured": 8,
        },
        sections_text=[
            "Getting started | uv — Installing uv / https://docs.astral.sh/uv/ Install uv via standalone installer.",
            "Getting started | uv — First steps / https://docs.astral.sh/uv/ After installing uv, run uv to verify the installation worked.",
            "Features | uv — Python versions / https://docs.astral.sh/uv/ uv installs and manages Python versions automatically.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "uv-docs", skill_name="uv-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    assert "a docs site root URL" in desc
    assert "auto-generated from documentation_site source" in desc
    assert "how do i use" in desc.lower()
    assert "docs" in desc.lower()


def test_video_section_labels_use_mm_ss_timestamps(tmp_path, monkeypatch) -> None:
    """Video sections render as '§N (m:ss–m:ss)'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="vid-labels")
    result = ss.compose_skill_scaffold_from_playbook(
        "vid-labels", skill_name="vid-labels-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**§1 (0:00–1:00)**" in body
    assert "?:??" not in body  # the broken fallback must not appear


def test_pdf_section_labels_use_page_numbers(tmp_path, monkeypatch) -> None:
    """PDF sections render as '§N (page N)' with the page parsed from text."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="pdf-labels",
        source_kind="pdf",
        title="Some PDF",
        capture_config_extras={"sourcePath": "C:\\x.pdf", "pageCount": 2},
        sections_text=[
            "Page 1 / page 1 / file:// First page body content here.",
            "Page 2 / page 2 / file:// Second page body content here.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "pdf-labels", skill_name="pdf-labels-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**§1 (page 1)**" in body
    assert "**§2 (page 2)**" in body
    assert "?:??" not in body


def test_web_section_labels_use_heading_inline(tmp_path, monkeypatch) -> None:
    """Web sections render as '§N — <heading>' (heading extracted from before ' / ')."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="web-labels",
        source_kind="web_page",
        title="Some Web Page",
        sections_text=[
            "Introduction / https://example.com/ Intro body text here.",
            "Configuration / https://example.com/ Config body text here.",
        ],
        section_word_counts=[5, 5],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "web-labels", skill_name="web-labels-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**§1 — Introduction**" in body
    assert "**§2 — Configuration**" in body
    assert "?:??" not in body


def test_docs_section_labels_strip_page_title_prefix(tmp_path, monkeypatch) -> None:
    """Docs sections render as '§N — <section_heading>' (text after ' — ' in the first segment)."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="docs-labels",
        source_kind="documentation_site",
        title="Some Docs",
        capture_config_extras={"seedUrl": "https://docs.example.com/", "pagesCaptured": 2},
        sections_text=[
            "Getting started | uv — Installing uv / https://docs.astral.sh/uv/ Install body.",
            "Getting started | uv — First steps / https://docs.astral.sh/uv/ First steps body.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "docs-labels", skill_name="docs-labels-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**§1 — Installing uv**" in body
    assert "**§2 — First steps**" in body
    assert "?:??" not in body


def test_video_source_block_uses_video_label_with_duration(tmp_path, monkeypatch) -> None:
    """Video source block contains 'Video: <title> (<duration> min)'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="vid-src")
    result = ss.compose_skill_scaffold_from_playbook(
        "vid-src", skill_name="vid-src-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**Video:** Fake video vid-src (5 min)" in body


def test_web_source_block_uses_source_page_with_word_count(tmp_path, monkeypatch) -> None:
    """Web source block contains 'Source page: <URL> (<N> words)' and no 'Video:' line."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="web-src",
        source_kind="web_page",
        title="Some Web Page",
        source_url="https://example.com/page",
        sections_text=[
            "Section A / https://example.com/page body A.",
            "Section B / https://example.com/page body B.",
        ],
        section_word_counts=[100, 150],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "web-src", skill_name="web-src-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**Source page:** https://example.com/page (250 words)" in body
    assert "**Video:**" not in body


def test_pdf_source_block_uses_source_pdf_with_page_count(tmp_path, monkeypatch) -> None:
    """PDF source block contains 'Source PDF: <filename> (<N> pages)' and no 'Video:' line."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="pdf-src",
        source_kind="pdf",
        title="Some PDF",
        capture_config_extras={
            "sourcePath": "C:\\Users\\test\\Downloads\\Some PDF.pdf",
            "pageCount": 7,
        },
        sections_text=["Page 1 / page 1 / file:// body."],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "pdf-src", skill_name="pdf-src-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**Source PDF:** Some PDF.pdf (7 pages)" in body
    assert "**Video:**" not in body


def test_docs_source_block_uses_source_docs_root_with_page_count(tmp_path, monkeypatch) -> None:
    """Docs source block contains 'Source docs root: <URL> (<N> pages)' and no 'Video:' line."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="docs-src",
        source_kind="documentation_site",
        title="Some Docs",
        capture_config_extras={
            "seedUrl": "https://docs.example.com/getting-started/",
            "pagesCaptured": 12,
        },
        sections_text=["Getting started — Install / https://docs.example.com/ body."],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "docs-src", skill_name="docs-src-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "**Source docs root:** https://docs.example.com/getting-started/ (12 pages)" in body
    assert "**Video:**" not in body


def test_url_fragments_do_not_become_keywords(tmp_path, monkeypatch) -> None:
    """URLs in section text must not leak into keyword extraction as 'https developer' etc."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="url-noise",
        source_kind="web_page",
        title="Some Page",
        sections_text=[
            "Authentication / https://stripe.com/docs/api/authentication API keys protect Stripe account security credentials.",
            "Authentication / https://stripe.com/docs/api/authentication API keys grant request authorization across endpoints.",
            "Authentication / https://stripe.com/docs/api/authentication Use Bearer header for API keys in every request.",
        ],
        section_word_counts=[12, 11, 13],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "url-noise", skill_name="url-noise-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    # The Covers list should not include URL fragments.
    forbidden = ["https", "http", "www", "com docs", "stripe com", "docs api"]
    for f in forbidden:
        assert f not in desc, f"keyword extraction leaked URL fragment '{f}' into description: {desc}"


def test_youtube_outro_spam_filtered_from_keywords(tmp_path, monkeypatch) -> None:
    """Subscribe/like/channel-thanks tokens must not surface as keywords for short videos."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(
        tmp_path,
        name="outro-spam",
        sections_text=[
            "Kubernetes is a container orchestration platform for deploying workloads at scale.",
            "Deploy pods using kubectl apply with a deployment manifest yaml file.",
            "Thanks for watching subscribe and like and stay tuned for more kubernetes content coming soon.",
            "Hit the subscribe button and check out the channel for more videos coming soon.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "outro-spam", skill_name="outro-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"].lower()
    for outro in ["subscribe", "channel soon", "tuned", "thanks watching", "coming soon"]:
        assert outro not in desc, f"outro spam '{outro}' leaked into description: {desc}"


def test_docs_topic_falls_back_to_seed_url_when_title_is_generic(tmp_path, monkeypatch) -> None:
    """Docs site with a generic page title ('Getting started') derives topic from seedUrl path."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="generic-title",
        source_kind="documentation_site",
        title="Getting started | uv",  # _topic_from_title splits to "Getting started"
        source_url="https://docs.astral.sh/uv/getting-started/",
        capture_config_extras={
            "seedUrl": "https://docs.astral.sh/uv/getting-started/",
            "pagesCaptured": 5,
        },
        sections_text=[
            "Getting started — Installing uv / https://docs.astral.sh/uv/ Install uv body text.",
            "Getting started — First steps / https://docs.astral.sh/uv/ First steps body text.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "generic-title", skill_name="uv-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    # Topic should be "uv" (from seed-URL path segment), not "Getting started".
    assert "about uv," in desc
    assert "'how do I use uv'" in desc
    assert "Getting started" not in desc.split("Covers")[0]


def test_docs_topic_keeps_specific_title_over_seed_url(tmp_path, monkeypatch) -> None:
    """When a docs page has a non-generic title, the title wins — don't second-guess it."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="specific-title",
        source_kind="documentation_site",
        title="Webhook signatures",
        capture_config_extras={
            "seedUrl": "https://stripe.com/docs/webhooks/signatures",
            "pagesCaptured": 3,
        },
        sections_text=[
            "Webhook signatures / https://stripe.com/ Verify webhook signatures using Stripe-Signature header.",
        ],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "specific-title", skill_name="webhook-skill", skills_root=str(tmp_path / "project")
    )
    assert "about Webhook signatures," in result["triggerDescription"]


def test_identical_token_bigrams_are_dropped_from_keywords(tmp_path, monkeypatch) -> None:
    """Bigrams of two identical tokens ('semantics semantics') are heading-repetition noise."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    # Repeat the same heading word across many sections so 'semantics semantics' would
    # otherwise dominate the bigram counts.
    _write_document_playbook(
        tmp_path,
        name="dup-bigram",
        source_kind="web_page",
        title="HTTP Semantics",
        sections_text=[
            "Semantics Semantics / https://example.com/ Request method semantics overview.",
            "Semantics Semantics / https://example.com/ GET request method semantics.",
            "Semantics Semantics / https://example.com/ POST request method semantics.",
            "Semantics Semantics / https://example.com/ PUT request method semantics.",
        ],
        section_word_counts=[10, 10, 10, 10],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "dup-bigram", skill_name="dup-skill", skills_root=str(tmp_path / "project")
    )
    desc = result["triggerDescription"]
    assert "semantics semantics" not in desc.lower()
    # Sanity: actually pulled real bigrams.
    assert "covering " in desc and "covering the concepts covered in the source" not in desc


def test_knows_preamble_is_source_kind_aware(tmp_path, monkeypatch) -> None:
    """The 'What this skill knows' preamble names the source type, not 'video' universally."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_document_playbook(
        tmp_path,
        name="pdf-pre",
        source_kind="pdf",
        title="Some PDF",
        capture_config_extras={"sourcePath": "C:\\x.pdf", "pageCount": 1},
        sections_text=["Page 1 / page 1 / file:// body."],
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "pdf-pre", skill_name="pdf-pre-skill", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "one page of the source PDF" in body
    assert "video timestamp" not in body


def test_scope_notes_appear_in_source_notes_block(tmp_path, monkeypatch) -> None:
    """Author-supplied scope notes are rendered verbatim into the source notes block."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="pb2")
    result = ss.compose_skill_scaffold_from_playbook(
        "pb2",
        skill_name="with-notes",
        scope_notes="Only run during business hours; ask before sending any external message.",
        skills_root=str(tmp_path / "project"),
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "Only run during business hours" in body
    assert "Author scope notes" in body


# ---------------------------------------------------------------------------
# Shape detection: skill vs. agent
# ---------------------------------------------------------------------------


def _write_curriculum_playbook(
    tmp_path: Path,
    *,
    name: str,
    title: str,
    section_count: int = 20,
) -> Path:
    """Curriculum-shaped playbook: many sections, role-word title.

    Used to exercise the agent-shape detection path.
    """
    playbook_dir = tmp_path / name
    playbook_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "slug": name,
        "sourceUrl": f"https://youtu.be/{name}",
        "video": {"title": title, "channel": "tester", "durationSeconds": 28 * 3600},
        "summary": None,
    }
    sections = [
        {
            "ordinal": i + 1,
            "videoStartSeconds": i * 600.0,
            "videoEndSeconds": (i + 1) * 600.0,
            "text": f"Module {i + 1} covering SQL, Python, and dashboards.",
            "anchorKeyframePath": f"keyframes/{i+1:03d}.jpg",
        }
        for i in range(section_count)
    ]
    lessons = {
        "name": name,
        "sourceUrl": manifest["sourceUrl"],
        "video": manifest["video"],
        "sectionCount": len(sections),
        "sections": sections,
    }
    (playbook_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (playbook_dir / "lessons.json").write_text(json.dumps(lessons), encoding="utf-8")
    return playbook_dir


def test_detect_shape_bootcamp_title_with_many_sections_returns_agent() -> None:
    """A 'Bootcamp' title with >= 10 sections should be detected as an agent."""
    manifest = {"video": {"title": "2026 FREE Data Analyst Bootcamp [24 Hours+]"}}
    sections = [{"ordinal": i} for i in range(20)]
    assert ss._detect_shape(manifest, sections) == "agent"


def test_detect_shape_short_video_with_role_word_falls_back_to_skill() -> None:
    """A short 'X Course in 100 Seconds' is a skill, not an agent — section count blocks it."""
    manifest = {"video": {"title": "Docker Full Course in 100 Seconds"}}
    sections = [{"ordinal": i} for i in range(6)]
    assert ss._detect_shape(manifest, sections) == "skill"


def test_detect_shape_no_role_words_returns_skill() -> None:
    """Even with many sections, a non-role title stays a skill (e.g. uv docs, 25 sections)."""
    manifest = {"video": {"title": "Getting started | uv"}}
    sections = [{"ordinal": i} for i in range(25)]
    assert ss._detect_shape(manifest, sections) == "skill"


def test_detect_shape_picks_up_curriculum_keyword() -> None:
    """The 'curriculum' role-word is enough when section count is high."""
    manifest = {"video": {"title": "Complete Data Analyst Curriculum 2026"}}
    sections = [{"ordinal": i} for i in range(15)]
    assert ss._detect_shape(manifest, sections) == "agent"


def test_compose_auto_shape_writes_agent_md_for_bootcamp(tmp_path, monkeypatch) -> None:
    """A bootcamp playbook composed with shape='auto' lands at .claude/agents/<slug>.md."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="da-bootcamp",
        title="2026 FREE Data Analyst Bootcamp [24 Hours+]",
        section_count=20,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "da-bootcamp",
        skill_name="data-analyst-agent",
        skills_root=str(tmp_path / "project"),
    )
    assert result["shape"] == "agent"
    assert result["shapeResolvedFrom"] == "heuristic"
    assert result["skillPath"] is None
    agent_md = Path(result["agentPath"])
    assert agent_md.exists()
    assert agent_md.name == "data-analyst-agent.md"
    # Lives in .claude/agents/, NOT .claude/skills/<slug>/
    assert agent_md.parent.name == "agents"
    assert agent_md.parent.parent.name == ".claude"


def test_compose_agent_scaffold_uses_role_shape_sections(tmp_path, monkeypatch) -> None:
    """Agent scaffold has Role + Curriculum + Owned skills + When to invoke + governance sections — not 'How to apply'."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="agent-shape",
        title="Become a Data Engineer Roadmap",
        section_count=15,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "agent-shape",
        skill_name="de-agent",
        skills_root=str(tmp_path / "project"),
    )
    body = Path(result["agentPath"]).read_text(encoding="utf-8")
    assert "## Role" in body
    assert "## Curriculum" in body
    assert "## Owned skills" in body
    assert "## When to invoke this agent" in body
    assert "orchestrating agent" in body.lower()
    # Skill-shaped headings should NOT appear in the agent scaffold
    assert "## How to apply" not in body
    assert "## What this skill knows" not in body


def test_compose_shape_skill_override_forces_skill_md_even_on_bootcamp(tmp_path, monkeypatch) -> None:
    """shape='skill' overrides the heuristic — even a bootcamp playbook lands as SKILL.md."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="force-skill",
        title="Data Analyst Bootcamp Full Curriculum",
        section_count=25,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "force-skill",
        skill_name="forced-skill",
        shape="skill",
        skills_root=str(tmp_path / "project"),
    )
    assert result["shape"] == "skill"
    assert result["shapeResolvedFrom"] == "explicit"
    assert result["agentPath"] is None
    skill_md = Path(result["skillPath"])
    assert skill_md.exists()
    assert skill_md.name == "SKILL.md"
    assert "## How to apply" in skill_md.read_text(encoding="utf-8")


def test_compose_shape_agent_override_forces_agent_md_even_on_short_video(tmp_path, monkeypatch) -> None:
    """shape='agent' overrides the heuristic — even a short Fireship video becomes an agent."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="short-vid")
    result = ss.compose_skill_scaffold_from_playbook(
        "short-vid",
        skill_name="forced-agent",
        shape="agent",
        skills_root=str(tmp_path / "project"),
    )
    assert result["shape"] == "agent"
    assert result["shapeResolvedFrom"] == "explicit"
    assert "## Owned skills" in Path(result["agentPath"]).read_text(encoding="utf-8")


def test_compose_invalid_shape_raises(tmp_path, monkeypatch) -> None:
    """Unknown shape value is rejected with a clear error."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="pb-shape")
    with pytest.raises(ss.SkillSynthesisError, match="invalid shape"):
        ss.compose_skill_scaffold_from_playbook(
            "pb-shape",
            skill_name="bad-shape",
            shape="orchestrator",
            skills_root=str(tmp_path / "project"),
        )


def test_compose_agent_response_mandates_codify_for_orchestration(tmp_path, monkeypatch) -> None:
    """Agent nextStep references the new owned-skills + governance stubs, not the 'How to apply' stub."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="codify-agent",
        title="Career Switch to Data Analyst Bootcamp",
        section_count=20,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "codify-agent",
        skill_name="career-agent",
        skills_root=str(tmp_path / "project"),
    )
    assert result["shape"] == "agent"
    assert "Owned skills" in result["nextStep"]
    assert "When to invoke this agent" in result["nextStep"]
    assert "Constraints" in result["nextStep"]
    assert "Error handling" in result["nextStep"]
    assert "How to apply" not in result["nextStep"]
    assert "NOT a complete agent" in result["criticalRule"]


def test_compose_agent_description_mentions_orchestration(tmp_path, monkeypatch) -> None:
    """Auto-generated agent description names the role-shape, not the procedure-shape."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="desc-agent",
        title="2026 Data Analyst Bootcamp",
        section_count=15,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "desc-agent",
        skill_name="da-agent",
        skills_root=str(tmp_path / "project"),
    )
    desc = result["triggerDescription"]
    assert "Orchestrating agent" in desc or "orchestrating agent" in desc.lower()
    # Skill list is NOT hardcoded — must point at the Owned skills section, not name skills
    assert "Owned skills" in desc
    assert "sql-tsql" not in desc  # no hardcoded boilerplate
    assert "python-pandas" not in desc
    assert "become a" in desc.lower()


# ---------------------------------------------------------------------------
# Topic / title stripping (governance audit fix #1)
# ---------------------------------------------------------------------------


def test_topic_strips_year_prefix() -> None:
    """A title like '2026 Data Analyst Bootcamp' becomes 'Data Analyst'."""
    assert ss._topic_from_title("2026 Data Analyst Bootcamp") == "Data Analyst"


def test_topic_strips_free_decoration() -> None:
    """The marketing word FREE is stripped wherever it appears."""
    out = ss._topic_from_title("FREE Python Course for FREE")
    assert "FREE" not in out
    assert "Python" in out


def test_topic_strips_hours_bracket() -> None:
    """[24 Hours+] / [24+ Hours] decorations are stripped."""
    assert "Hours" not in ss._topic_from_title("Data Analyst Bootcamp [24 Hours+]")
    assert "Hours" not in ss._topic_from_title("Data Analyst Course [24+ Hours]")


def test_topic_strips_bootcamp_suffix() -> None:
    """Role-shape words at the end of a title aren't part of the topic."""
    assert ss._topic_from_title("Data Analyst Bootcamp") == "Data Analyst"
    assert ss._topic_from_title("Data Engineer Roadmap") == "Data Engineer"


def test_topic_from_real_bootcamp_title_yields_clean_role() -> None:
    """The real-world bootcamp title strips to 'Data Analyst', not the full marketing string."""
    title = "2026 FREE Data Analyst Bootcamp [24 Hours+] for FREE | SQL, Excel, Python, Power BI, GitHub, AWS"
    out = ss._topic_from_title(title)
    assert out == "Data Analyst", f"expected 'Data Analyst', got: {out!r}"


def test_agent_description_uses_cleaned_topic_not_verbose_title(tmp_path, monkeypatch) -> None:
    """The agent description references the cleaned topic, not the raw verbose title."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="clean-topic",
        title="2026 FREE Data Analyst Bootcamp [24 Hours+] for FREE",
        section_count=15,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "clean-topic",
        skill_name="da-agent-clean",
        skills_root=str(tmp_path / "project"),
    )
    desc = result["triggerDescription"]
    # The verbose decorations must NOT appear in the description
    assert "FREE" not in desc
    assert "Hours+" not in desc
    assert "2026" not in desc
    assert "Bootcamp" not in desc
    # The cleaned topic must appear
    assert "Data Analyst" in desc


# ---------------------------------------------------------------------------
# Skill governance sections (audit fix #2)
# ---------------------------------------------------------------------------


def test_skill_scaffold_frontmatter_is_name_and_description_only(tmp_path, monkeypatch) -> None:
    """YAML frontmatter has only name/description (the official Agent Skills spec fields).

    Typed inputs/outputs/dependencies are declared in the body sections
    instead — see test_skill_scaffold_has_inputs_outputs_success_failure_dependencies_sections.
    """
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="govern-skill")
    result = ss.compose_skill_scaffold_from_playbook(
        "govern-skill", skill_name="govern", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    # Extract the YAML frontmatter
    parts = body.split("---", 2)
    assert len(parts) >= 3, "scaffold must have YAML frontmatter"
    frontmatter = parts[1]
    assert "name:" in frontmatter
    assert "description:" in frontmatter
    assert "inputs" not in frontmatter
    assert "outputs" not in frontmatter
    assert "dependencies" not in frontmatter


def test_skill_scaffold_has_inputs_outputs_success_failure_dependencies_sections(tmp_path, monkeypatch) -> None:
    """Skill scaffold ships with all six governance sections as /codify stubs."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="six-sections")
    result = ss.compose_skill_scaffold_from_playbook(
        "six-sections", skill_name="six", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    for heading in (
        "## Inputs",
        "## Outputs",
        "## How to apply",
        "## Success criteria",
        "## Failure modes",
        "## Dependencies",
    ):
        assert heading in body, f"missing section: {heading}"


def test_skill_scaffold_codify_dependency_is_documented(tmp_path, monkeypatch) -> None:
    """The Source notes section explicitly names /codify as a Periphery dependency."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="codify-doc")
    result = ss.compose_skill_scaffold_from_playbook(
        "codify-doc", skill_name="cdoc", skills_root=str(tmp_path / "project")
    )
    body = Path(result["skillPath"]).read_text(encoding="utf-8")
    assert "Codify dependency" in body
    assert "Periphery" in body


def test_skill_critical_rule_lists_all_stub_sections(tmp_path, monkeypatch) -> None:
    """criticalRule enumerates the six stub sections that ship as placeholders."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="crit-rule")
    result = ss.compose_skill_scaffold_from_playbook(
        "crit-rule", skill_name="cr-skill", skills_root=str(tmp_path / "project")
    )
    crit = result["criticalRule"]
    assert "Six required" in crit
    assert "Inputs" in crit
    assert "Outputs" in crit
    assert "Success criteria" in crit
    assert "Failure modes" in crit
    assert "Dependencies" in crit


# ---------------------------------------------------------------------------
# Agent governance sections (audit fix #3)
# ---------------------------------------------------------------------------


def test_agent_scaffold_frontmatter_is_name_and_description_only(tmp_path, monkeypatch) -> None:
    """Agent YAML frontmatter has only name/description, not inputs/outputs/owned_skills.

    Those are declared in the body sections instead — see
    test_agent_scaffold_has_constraints_and_error_handling_sections.
    """
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="govern-agent",
        title="Data Engineer Bootcamp",
        section_count=15,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "govern-agent", skill_name="ga", skills_root=str(tmp_path / "project")
    )
    body = Path(result["agentPath"]).read_text(encoding="utf-8")
    parts = body.split("---", 2)
    frontmatter = parts[1]
    assert "name:" in frontmatter
    assert "description:" in frontmatter
    assert "inputs" not in frontmatter
    assert "outputs" not in frontmatter
    assert "owned_skills" not in frontmatter


def test_agent_scaffold_has_constraints_and_error_handling_sections(tmp_path, monkeypatch) -> None:
    """Agent scaffold ships with Constraints + Error handling + Inputs + Outputs sections."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="agent-gov",
        title="Data Analyst Bootcamp",
        section_count=15,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "agent-gov", skill_name="ag", skills_root=str(tmp_path / "project")
    )
    body = Path(result["agentPath"]).read_text(encoding="utf-8")
    for heading in (
        "## Inputs",
        "## Outputs",
        "## Owned skills",
        "## When to invoke this agent",
        "## Constraints",
        "## Error handling",
    ):
        assert heading in body, f"missing agent section: {heading}"


def test_agent_owned_skills_section_declares_table_schema(tmp_path, monkeypatch) -> None:
    """The Owned skills stub includes the required table column schema for /codify to fill."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="schema-agent",
        title="Data Analyst Bootcamp",
        section_count=12,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "schema-agent", skill_name="sa", skills_root=str(tmp_path / "project")
    )
    body = Path(result["agentPath"]).read_text(encoding="utf-8")
    # The schema table header declares the contract for /codify
    assert "| Skill | Curriculum section(s) | When to delegate | Input handoff | Output expected |" in body


# ---------------------------------------------------------------------------
# Workflow shape (audit fix #4 — orchestration document)
# ---------------------------------------------------------------------------


def test_workflow_shape_writes_to_claude_workflows_dir(tmp_path, monkeypatch) -> None:
    """shape='workflow' lands at .claude/workflows/<slug>.md, not skills/ or agents/."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="wf-pb")
    result = ss.compose_skill_scaffold_from_playbook(
        "wf-pb",
        skill_name="my-workflow",
        shape="workflow",
        skills_root=str(tmp_path / "project"),
    )
    assert result["shape"] == "workflow"
    assert result["shapeResolvedFrom"] == "explicit"
    # workflowPath populated, skillPath and agentPath null
    assert result["skillPath"] is None
    assert result["agentPath"] is None
    wf = Path(result["workflowPath"])
    assert wf.exists()
    assert wf.parent.name == "workflows"
    assert wf.parent.parent.name == ".claude"
    assert wf.name == "my-workflow.md"


def test_workflow_scaffold_has_steps_decision_gates_data_flow_rollback(tmp_path, monkeypatch) -> None:
    """Workflow scaffold ships with all four orchestration governance sections."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="wf-sections")
    result = ss.compose_skill_scaffold_from_playbook(
        "wf-sections", skill_name="wfs", shape="workflow", skills_root=str(tmp_path / "project")
    )
    body = Path(result["workflowPath"]).read_text(encoding="utf-8")
    for heading in (
        "## Inputs",
        "## Outputs",
        "## Steps",
        "## Decision gates",
        "## Data flow",
        "## Rollback",
        "## Curriculum reference",
    ):
        assert heading in body, f"missing workflow section: {heading}"


def test_workflow_steps_table_declares_schema(tmp_path, monkeypatch) -> None:
    """The Steps stub includes the required column schema (# / Skill / Input / Output / Success gate / On failure)."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="wf-schema")
    result = ss.compose_skill_scaffold_from_playbook(
        "wf-schema", skill_name="wfsch", shape="workflow", skills_root=str(tmp_path / "project")
    )
    body = Path(result["workflowPath"]).read_text(encoding="utf-8")
    assert "| # | Skill | Input (from) | Output (to) | Success gate | On failure |" in body


def test_workflow_owner_agent_passed_through_to_frontmatter(tmp_path, monkeypatch) -> None:
    """owner_agent='some-agent' lands in the YAML frontmatter."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="wf-owner")
    result = ss.compose_skill_scaffold_from_playbook(
        "wf-owner",
        skill_name="wfowner",
        shape="workflow",
        owner_agent="data-analyst",
        skills_root=str(tmp_path / "project"),
    )
    assert result["ownerAgent"] == "data-analyst"
    body = Path(result["workflowPath"]).read_text(encoding="utf-8")
    assert "owner_agent: data-analyst" in body


def test_workflow_owner_agent_defaults_to_null(tmp_path, monkeypatch) -> None:
    """Without owner_agent, the frontmatter records `null` and source-notes warn it's unreachable."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="wf-noowner")
    result = ss.compose_skill_scaffold_from_playbook(
        "wf-noowner", skill_name="wfno", shape="workflow", skills_root=str(tmp_path / "project")
    )
    assert result["ownerAgent"] is None
    body = Path(result["workflowPath"]).read_text(encoding="utf-8")
    assert "owner_agent: null" in body
    assert "unreachable from the agent layer" in body


def test_workflow_shape_is_not_auto_selected_by_heuristic(tmp_path, monkeypatch) -> None:
    """Even a bootcamp-shaped curriculum playbook auto-detects as agent, not workflow."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_curriculum_playbook(
        tmp_path,
        name="not-wf",
        title="Data Analyst Bootcamp Curriculum",
        section_count=25,
    )
    result = ss.compose_skill_scaffold_from_playbook(
        "not-wf",
        skill_name="auto-shape",
        skills_root=str(tmp_path / "project"),
    )
    # heuristic picks agent, never workflow
    assert result["shape"] == "agent"


def test_workflow_critical_rule_enumerates_six_stubs(tmp_path, monkeypatch) -> None:
    """criticalRule for workflows names all six required stub sections."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="wf-crit")
    result = ss.compose_skill_scaffold_from_playbook(
        "wf-crit", skill_name="wfc", shape="workflow", skills_root=str(tmp_path / "project")
    )
    crit = result["criticalRule"]
    assert "Inputs" in crit
    assert "Outputs" in crit
    assert "Steps" in crit
    assert "Decision gates" in crit
    assert "Data flow" in crit
    assert "Rollback" in crit


def test_compose_shape_invalid_rejects_workflow_typo(tmp_path, monkeypatch) -> None:
    """A bad shape like 'workflows' (plural) is rejected with a clear error listing valid values."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="bad-shape")
    with pytest.raises(ss.SkillSynthesisError) as exc_info:
        ss.compose_skill_scaffold_from_playbook(
            "bad-shape", skill_name="bs", shape="workflows", skills_root=str(tmp_path / "project")
        )
    msg = str(exc_info.value)
    assert "auto" in msg
    assert "skill" in msg
    assert "agent" in msg
    assert "workflow" in msg
