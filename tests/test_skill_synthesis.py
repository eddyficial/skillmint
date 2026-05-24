"""Tests for periscribe.skill_synthesis (scaffold composition from playbooks).

The tests fully mock the playbook on disk so they don't depend on a captured tutorial.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from periscribe import skill_synthesis as ss


def _write_playbook(
    tmp_path: Path,
    *,
    name: str = "fake-tutorial",
    sections_text: list[str] | None = None,
    summary: str | None = None,
) -> Path:
    """Write a minimal manifest.json + lessons.json under tmp_path/<name>/."""
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


def test_compose_scaffold_writes_skill_md(tmp_path, monkeypatch) -> None:
    """Happy path: scaffold lands at .claude/skills/<slug>/SKILL.md with all expected blocks."""
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))
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


def test_compose_scaffold_default_trigger_uses_video_title(tmp_path, monkeypatch) -> None:
    """When trigger_description is omitted, the default references the source video title."""
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))
    _write_playbook(tmp_path, name="trigger-default")
    result = ss.compose_skill_scaffold_from_playbook(
        "trigger-default",
        skill_name="auto-trigger",
        skills_root=str(tmp_path / "project"),
    )
    assert "Fake video trigger-default" in result["triggerDescription"]


def test_compose_scaffold_refuses_to_overwrite_without_flag(tmp_path, monkeypatch) -> None:
    """A second compose call against the same skill name fails unless overwrite=True."""
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))
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
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))
    with pytest.raises(ss.SkillSynthesisError, match="not found"):
        ss.compose_skill_scaffold_from_playbook(
            "does-not-exist", skill_name="x", skills_root=str(tmp_path / "project")
        )


def test_compose_scaffold_rejects_undistilled_playbook(tmp_path, monkeypatch) -> None:
    """If lessons.json is missing the caller is told to distill first."""
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))
    pb = tmp_path / "raw"
    pb.mkdir()
    (pb / "manifest.json").write_text(json.dumps({"name": "raw"}), encoding="utf-8")
    with pytest.raises(ss.SkillSynthesisError, match="distill"):
        ss.compose_skill_scaffold_from_playbook(
            "raw", skill_name="x", skills_root=str(tmp_path / "project")
        )


def test_scope_notes_appear_in_source_notes_block(tmp_path, monkeypatch) -> None:
    """Author-supplied scope notes are rendered verbatim into the source notes block."""
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))
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
