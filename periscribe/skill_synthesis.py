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
import threading
from pathlib import Path
from typing import Any

from .tutorial_playbooks import _playbook_dir, _slugify

_lock = threading.RLock()


class SkillSynthesisError(RuntimeError):
    """Raised when a skill scaffold cannot be composed."""


def _skill_dir(skill_slug: str, *, base: Path | None = None) -> Path:
    """Return the on-disk location for a project-local skill."""
    root = base if base is not None else Path.cwd()
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


def _render_scaffold_markdown(
    *,
    skill_name: str,
    playbook_meta: dict[str, Any],
    sections: list[dict[str, Any]],
    trigger_description: str,
    scope_notes: str | None,
) -> str:
    """Build the SKILL.md text from the playbook content and trigger metadata."""
    source_url = playbook_meta.get("sourceUrl") or "(unknown)"
    video = playbook_meta.get("video") or {}
    video_title = video.get("title") or playbook_meta.get("name") or "(unknown)"
    duration = video.get("durationSeconds")
    duration_label = (
        f"{int(duration) // 60} min" if isinstance(duration, (int, float)) else "?"
    )

    lines: list[str] = ["---", f"name: {skill_name}", f"description: {trigger_description}", "---", ""]
    lines.append(f"# {skill_name}")
    lines.append("")

    lines.append("## Source playbook")
    lines.append("")
    lines.append(f"- **Playbook:** `{playbook_meta.get('name')}`")
    lines.append(f"- **Video:** {video_title} ({duration_label})")
    lines.append(f"- **Source URL:** {source_url}")
    if playbook_meta.get("summary"):
        lines.append("")
        lines.append("**Playbook summary (author-supplied):**")
        lines.append("")
        lines.append(str(playbook_meta["summary"]))
    lines.append("")

    lines.append("## What this skill knows")
    lines.append("")
    lines.append(f"Distilled from {len(sections)} sections of the source playbook. "
                 "Each row below is one topical chunk of the tutorial with its video "
                 "timestamp, so claims can be traced back to the source.")
    lines.append("")
    for section in sections:
        ordinal = section.get("ordinal", "?")
        start = section.get("videoStartSeconds")
        end = section.get("videoEndSeconds")
        snippet = _truncate_words((section.get("text") or "").strip(), 30)
        if not snippet:
            snippet = "_(no caption text)_"
        lines.append(f"- **§{ordinal} ({_mm_ss(start)}–{_mm_ss(end)})** — {snippet}")
    lines.append("")

    lines.append("## How to apply")
    lines.append("")
    lines.append("_(This section is a stub. Run `/codify " + skill_name +
                 "` in a Claude Code session to have the current agent read the "
                 "playbook's full lessons.md and turn the knowledge above into an "
                 "actionable procedure: ordered steps, the tools to call, and "
                 "verification checks. Edit by hand after that if you want.)_")
    lines.append("")

    lines.append("## Source notes")
    lines.append("")
    lines.append("- This skill carries the authority of one tutorial author. Cite the source section when applying claims so the user can trace them back.")
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

    The scaffold lists every distilled section as a bullet (with timestamp and
    snippet) and stubs out a "How to apply" block for the current Claude
    Code session to fill in via /codify. Returns the on-disk path and
    metadata about the composed scaffold.
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
        video_title = (manifest.get("video") or {}).get("title") or playbook_name
        trigger_description = (
            f"Apply lessons composed from the tutorial: {video_title}. "
            "Invoke when the user asks about this topic."
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

    return {
        "ok": True,
        "skillName": skill_name,
        "skillSlug": skill_slug,
        "skillPath": str(skill_md_path),
        "skillDirectory": str(target),
        "sourcePlaybook": manifest.get("name"),
        "sectionCount": len(sections),
        "triggerDescription": trigger_description,
        "nextStep": (
            f"Run `/codify {skill_name}` in a Claude Code session to fill in "
            "the 'How to apply' section."
        ),
    }
