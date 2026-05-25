"""Persistent tutorial playbooks: save what an agent learned from a video, recall it later.

A tutorial playbook is the persisted form of a follow-along watch session. It
captures the step events (keyframes + transcript + captions), the source
video metadata, and an optional model-written summary. The agent can save a
playbook once after watching a tutorial, then in any future session call
``read_tutorial_playbook(name)`` to pull the steps back into context without
re-processing the video.

Storage layout (mirrors learned_playbooks):

    %USERPROFILE%/.periscribe/playbooks/<slug>/
      manifest.json    -- name, source URL, video metadata, created_at, step count, summary
      steps.json       -- ordered step records with relative keyframe paths
      transcript.md    -- full transcript across all steps (human-reviewable)
      keyframes/
        001.jpg
        002.jpg
        ...

Keyframes are stored as separate JPEG files so the manifest stays small and
agents can load only the keyframes they need via ``include_keyframes=True``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .live_video import LiveVideoError, get_session_step_snapshot

_VTT_KARAOKE_TIMESTAMP = re.compile(r"<\d+:\d+:\d+\.\d+>")
_VTT_INLINE_TAG = re.compile(r"</?c[^>]*>")
DEFAULT_SECTION_DIFF_SCORE = 60.0

_lock = threading.RLock()


class TutorialPlaybookError(RuntimeError):
    """Raised when a tutorial playbook operation fails in a recoverable way."""


def _store_dir() -> Path:
    """Return the on-disk directory that holds tutorial playbooks, created on demand."""
    override = os.environ.get("PERISCRIBE_PLAYBOOK_DIR")
    base = Path(override) if override else Path(os.path.expanduser("~")) / ".periscribe" / "playbooks"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _slugify(name: str) -> str:
    """Reduce a free-form playbook name to a filesystem-safe slug."""
    safe_chars: list[str] = []
    for ch in name.strip().lower():
        if ch.isalnum():
            safe_chars.append(ch)
        else:
            safe_chars.append("-")
    raw = "".join(safe_chars)
    # Collapse consecutive dashes so "foo / bar" doesn't slugify to "foo--bar".
    collapsed_parts = [part for part in raw.split("-") if part]
    slug = "-".join(collapsed_parts) or "tutorial"
    return slug[:80]


def _playbook_dir(name: str) -> Path:
    """Return the directory path for a named playbook (does not create it)."""
    return _store_dir() / _slugify(name)


def save_tutorial_as_playbook(
    session_id: str,
    name: str,
    *,
    overwrite: bool = False,
    summary: str | None = None,
    max_steps: int | None = None,
) -> dict[str, Any]:
    """Persist a live follow-along session's accumulated steps as a named playbook.

    Reads the current step ring out of the live video session, writes one JPEG
    per keyframe under keyframes/, a manifest.json, a steps.json, and a
    transcript.md. Returns the persisted manifest with the on-disk path.
    """
    try:
        snapshot = get_session_step_snapshot(session_id)
    except LiveVideoError as exc:
        raise TutorialPlaybookError(str(exc)) from exc
    steps: list[dict[str, Any]] = list(snapshot.get("steps") or [])
    if not steps:
        raise TutorialPlaybookError(
            f"session {session_id} has not emitted any step events yet; "
            "play more of the tutorial before saving."
        )
    return persist_playbook_from_snapshot(
        name,
        snapshot,
        overwrite=overwrite,
        summary=summary,
        max_steps=max_steps,
    )


def persist_playbook_from_snapshot(
    name: str,
    snapshot: dict[str, Any],
    *,
    overwrite: bool = False,
    summary: str | None = None,
    max_steps: int | None = None,
) -> dict[str, Any]:
    """Write a snapshot dict to a named playbook directory on disk.

    Shared by save_tutorial_as_playbook (live session) and the offline batch
    capture path. The snapshot must look like get_session_step_snapshot's
    return value: {sessionId, url, video, config, steps:[{..., keyframeJpeg}]}.
    """
    name = (name or "").strip()
    if not name:
        raise TutorialPlaybookError("name is required")
    steps: list[dict[str, Any]] = list(snapshot.get("steps") or [])
    if not steps:
        raise TutorialPlaybookError("snapshot has no steps to persist")
    if max_steps is not None and len(steps) > int(max_steps):
        steps = steps[-int(max_steps):]

    target_dir = _playbook_dir(name)
    with _lock:
        if target_dir.exists():
            if not overwrite:
                raise TutorialPlaybookError(
                    f"tutorial playbook '{name}' already exists; pass overwrite=True to replace it."
                )
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True)
        keyframes_dir = target_dir / "keyframes"
        # Only create the keyframes/ dir if at least one step actually has bytes.
        any_keyframes = any((s.get("keyframeJpeg") or b"") for s in steps)
        if any_keyframes:
            keyframes_dir.mkdir(parents=True, exist_ok=True)

        persisted_steps: list[dict[str, Any]] = []
        for idx, step in enumerate(steps, start=1):
            jpeg = step.get("keyframeJpeg") or b""
            if jpeg:
                keyframe_path = keyframes_dir / f"{idx:03d}.jpg"
                keyframe_path.write_bytes(jpeg)
                keyframe_relpath: str | None = f"keyframes/{idx:03d}.jpg"
            else:
                keyframe_relpath = None
            persisted_steps.append(
                {
                    "ordinal": idx,
                    "sequence": step.get("sequence"),
                    "startedAt": step.get("startedAt"),
                    "endedAt": step.get("endedAt"),
                    "trigger": step.get("trigger"),
                    "diffScore": step.get("diffScore"),
                    "secondsSincePrevious": step.get("secondsSincePrevious"),
                    "videoStartSeconds": step.get("videoStartSeconds"),
                    "videoEndSeconds": step.get("videoEndSeconds"),
                    "keyframeRelativePath": keyframe_relpath,
                    "keyframeWidth": step.get("keyframeWidth"),
                    "keyframeHeight": step.get("keyframeHeight"),
                    "keyframeByteLength": len(jpeg),
                    "transcriptText": step.get("transcriptText") or "",
                    "captionText": step.get("captionText") or "",
                }
            )

        manifest = {
            "name": name,
            "slug": _slugify(name),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "sourceSessionId": snapshot.get("sessionId"),
            "sourceUrl": snapshot.get("url"),
            "video": snapshot.get("video") or {},
            "captureConfig": snapshot.get("config") or {},
            "stepCount": len(persisted_steps),
            "summary": (summary or "").strip() or None,
            "directory": str(target_dir),
        }
        (target_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        (target_dir / "steps.json").write_text(
            json.dumps({"steps": persisted_steps}, indent=2),
            encoding="utf-8",
        )
        (target_dir / "transcript.md").write_text(
            _render_transcript_markdown(name, manifest, persisted_steps, snapshot),
            encoding="utf-8",
        )

    return {
        "ok": True,
        "name": name,
        "slug": manifest["slug"],
        "directory": str(target_dir),
        "stepCount": len(persisted_steps),
        "createdAt": manifest["createdAt"],
        "sourceUrl": manifest["sourceUrl"],
        "summary": manifest["summary"],
    }


def _render_transcript_markdown(
    name: str,
    manifest: dict[str, Any],
    persisted_steps: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> str:
    """Render a human-readable markdown transcript for the playbook directory."""
    lines: list[str] = [f"# {name}", ""]
    video = manifest.get("video") or {}
    if video.get("title"):
        lines.append(f"- **Video:** {video.get('title')}")
    if manifest.get("sourceUrl"):
        lines.append(f"- **Source:** {manifest['sourceUrl']}")
    if video.get("channel"):
        lines.append(f"- **Channel:** {video.get('channel')}")
    if manifest.get("summary"):
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(str(manifest["summary"]))
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    for step in persisted_steps:
        lines.append(f"### Step {step['ordinal']} ({step.get('trigger', 'keyframe')})")
        if step.get("transcriptText"):
            lines.append("")
            lines.append(f"_Narration:_ {step['transcriptText']}")
        if step.get("captionText"):
            lines.append("")
            lines.append(f"_Captions:_ {step['captionText']}")
        lines.append("")
        if step.get("keyframeRelativePath"):
            lines.append(f"![Step {step['ordinal']}]({step['keyframeRelativePath']})")
            lines.append("")
    if snapshot.get("fullTranscriptText"):
        lines.append("## Full Transcript")
        lines.append("")
        lines.append(str(snapshot["fullTranscriptText"]))
        lines.append("")
    return "\n".join(lines)


def _merge_with_word_overlap(prev: str, part: str) -> tuple[str, bool]:
    """Try to merge ``part`` into ``prev`` by detecting a word-boundary overlap.

    YouTube's auto-caption rolling-window pattern emits successive fragments where
    each new fragment starts with the tail words of the previous one (e.g.
    ``"...you need to know to"`` followed by ``"to know everything you need to know
    to become..."``). We find the longest suffix of ``prev`` that is also a prefix
    of ``part`` (>=3 words) and merge by appending only the new tail.

    Returns ``(merged_text, True)`` if ``part`` was absorbed into ``prev``
    (exact duplicate, prefix containment, or overlapping tail), or
    ``(part, False)`` if no merge was possible and ``part`` should be a new entry.
    """
    if part == prev or prev.endswith(part):
        return prev, True
    if part.startswith(prev):
        return part, True
    prev_words = prev.split()
    part_words = part.split()
    max_words = min(len(prev_words), len(part_words), 40)
    for n in range(max_words, 2, -1):
        if prev_words[-n:] == part_words[:n]:
            tail_words = part_words[n:]
            if not tail_words:
                return prev, True
            return prev + " " + " ".join(tail_words), True
    return part, False


def _collapse_fragments(fragments: list[str]) -> str:
    """Reduce a sequence of caption fragments to one deduped string via word-overlap merge."""
    out: list[str] = []
    for raw in fragments:
        fragment = raw.strip()
        if not fragment:
            continue
        if not out:
            out.append(fragment)
            continue
        merged, absorbed = _merge_with_word_overlap(out[-1], fragment)
        if absorbed:
            out[-1] = merged
        else:
            out.append(fragment)
    return " ".join(out)


def _clean_caption_text(raw: str) -> str:
    """Strip VTT karaoke timing tags and collapse rolling-window duplicate fragments.

    The mechanical cleanup here is enough to turn 228 noisy step records into
    something an agent can read straight through without re-processing.
    """
    if not raw:
        return ""
    cleaned = _VTT_KARAOKE_TIMESTAMP.sub("", raw)
    cleaned = _VTT_INLINE_TAG.sub("", cleaned)
    lines = [line.strip() for line in cleaned.split("\n") if line.strip()]
    return _collapse_fragments(lines)


def _dedupe_section_text(parts: list[str]) -> str:
    """Concatenate per-step cleaned text, merging cross-step rolling overlap."""
    return _collapse_fragments(parts)


def _build_sections_from_steps(
    raw_steps: list[dict[str, Any]],
    *,
    section_diff_score: float = DEFAULT_SECTION_DIFF_SCORE,
) -> list[dict[str, Any]]:
    """Group consecutive steps into topical sections.

    A new section starts when a step has ``trigger=="quiet_period"`` (the host paused
    long enough to flush a quiet-period step) or its ``diffScore`` crosses
    ``section_diff_score`` (a hard visual cut, e.g. cutting from talking head to
    a chart). The first step always anchors section 1.
    """
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for step in raw_steps:
        trigger = step.get("trigger") or "keyframe"
        diff = step.get("diffScore")
        is_section_break = current is None
        if not is_section_break:
            if trigger == "quiet_period":
                is_section_break = True
            elif isinstance(diff, (int, float)) and diff >= section_diff_score:
                is_section_break = True
        if is_section_break:
            current = {
                "ordinal": len(sections) + 1,
                "startedAt": step.get("startedAt"),
                "videoStartSeconds": step.get("videoStartSeconds"),
                "videoEndSeconds": step.get("videoEndSeconds"),
                "trigger": trigger,
                "anchorKeyframePath": step.get("keyframeRelativePath"),
                "stepOrdinals": [step.get("ordinal")],
                "_cleaned_parts": [_clean_caption_text(step.get("captionText") or "")],
            }
            sections.append(current)
        else:
            assert current is not None
            current["stepOrdinals"].append(step.get("ordinal"))
            current["videoEndSeconds"] = step.get("videoEndSeconds") or current.get("videoEndSeconds")
            current["_cleaned_parts"].append(_clean_caption_text(step.get("captionText") or ""))
    # Collapse internal helper field into the public "text".
    for section in sections:
        text = _dedupe_section_text(section.pop("_cleaned_parts"))
        section["text"] = text
        section["wordCount"] = len(text.split()) if text else 0
    return sections


def _render_lessons_markdown(
    name: str,
    manifest: dict[str, Any],
    sections: list[dict[str, Any]],
) -> str:
    """Render the distilled lessons.md file."""
    lines: list[str] = [f"# {name} — distilled lessons", ""]
    video = manifest.get("video") or {}
    if video.get("title"):
        lines.append(f"- **Video:** {video['title']}")
    if manifest.get("sourceUrl"):
        lines.append(f"- **Source:** {manifest['sourceUrl']}")
    if manifest.get("summary"):
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(str(manifest["summary"]))
    lines.append("")
    lines.append(f"## Sections ({len(sections)})")
    lines.append("")
    for section in sections:
        start = section.get("videoStartSeconds") or 0
        end = section.get("videoEndSeconds") or start
        mm_ss = lambda s: f"{int(s)//60}:{int(s)%60:02d}"  # noqa: E731
        lines.append(f"### Section {section['ordinal']} — {mm_ss(start)} → {mm_ss(end)}")
        lines.append("")
        if section.get("anchorKeyframePath"):
            lines.append(f"![Section {section['ordinal']}]({section['anchorKeyframePath']})")
            lines.append("")
        if section.get("text"):
            lines.append(section["text"])
        else:
            lines.append("_(no caption text)_")
        lines.append("")
    return "\n".join(lines)


def distill_tutorial_playbook(
    name: str,
    *,
    section_diff_score: float = DEFAULT_SECTION_DIFF_SCORE,
) -> dict[str, Any]:
    """Strip karaoke noise from a saved playbook and group steps into topical sections.

    Writes ``lessons.md`` and ``lessons.json`` next to the existing manifest/steps.
    Pure mechanical cleanup — no LLM call — so it's fast and deterministic. The
    distilled output is what a /study skill loads when an agent needs to actually
    apply what the tutorial taught.
    """
    target_dir = _playbook_dir(name)
    manifest_path = target_dir / "manifest.json"
    steps_path = target_dir / "steps.json"
    if not manifest_path.exists() or not steps_path.exists():
        raise TutorialPlaybookError(f"tutorial playbook '{name}' not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_steps = json.loads(steps_path.read_text(encoding="utf-8")).get("steps") or []
    if not raw_steps:
        raise TutorialPlaybookError(
            f"tutorial playbook '{name}' has no steps to distill"
        )

    sections = _build_sections_from_steps(raw_steps, section_diff_score=section_diff_score)
    lessons_md = _render_lessons_markdown(name, manifest, sections)
    lessons_payload = {
        "name": manifest.get("name"),
        "slug": manifest.get("slug"),
        "sourceUrl": manifest.get("sourceUrl"),
        "video": manifest.get("video") or {},
        "summary": manifest.get("summary"),
        "distilledAt": datetime.now(timezone.utc).isoformat(),
        "stepCount": len(raw_steps),
        "sectionCount": len(sections),
        "sectionDiffScoreThreshold": section_diff_score,
        "sections": sections,
    }
    with _lock:
        (target_dir / "lessons.md").write_text(lessons_md, encoding="utf-8")
        (target_dir / "lessons.json").write_text(
            json.dumps(lessons_payload, indent=2),
            encoding="utf-8",
        )
    total_words = sum(section.get("wordCount", 0) for section in sections)
    return {
        "ok": True,
        "name": manifest.get("name"),
        "slug": manifest.get("slug"),
        "directory": str(target_dir),
        "stepCount": len(raw_steps),
        "sectionCount": len(sections),
        "totalWordCount": total_words,
        "lessonsMarkdownPath": str(target_dir / "lessons.md"),
        "lessonsJsonPath": str(target_dir / "lessons.json"),
    }


def list_tutorial_playbooks() -> dict[str, Any]:
    """List every saved tutorial playbook with its summary metadata."""
    with _lock:
        store = _store_dir()
        playbooks: list[dict[str, Any]] = []
        for child in sorted(store.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            playbooks.append(
                {
                    "name": manifest.get("name", child.name),
                    "slug": manifest.get("slug", child.name),
                    "createdAt": manifest.get("createdAt"),
                    "sourceUrl": manifest.get("sourceUrl"),
                    "videoTitle": (manifest.get("video") or {}).get("title"),
                    "stepCount": manifest.get("stepCount"),
                    "summary": manifest.get("summary"),
                    "directory": str(child),
                }
            )
    return {"count": len(playbooks), "playbooks": playbooks}


def read_tutorial_playbook(
    name: str,
    *,
    include_keyframes: bool = False,
    max_keyframes: int | None = None,
) -> dict[str, Any]:
    """Load a saved tutorial playbook, optionally including keyframe bytes."""
    import base64

    target_dir = _playbook_dir(name)
    manifest_path = target_dir / "manifest.json"
    steps_path = target_dir / "steps.json"
    if not manifest_path.exists() or not steps_path.exists():
        raise TutorialPlaybookError(f"tutorial playbook '{name}' not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    steps_payload = json.loads(steps_path.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = list(steps_payload.get("steps") or [])

    keyframe_load_limit = (
        len(steps)
        if max_keyframes is None
        else max(0, min(int(max_keyframes), len(steps)))
    )
    # Load keyframes for the most recent N steps (default = all when no cap).
    load_set = set(step["ordinal"] for step in steps[-keyframe_load_limit:]) if include_keyframes else set()

    enriched: list[dict[str, Any]] = []
    for step in steps:
        entry = dict(step)
        if step["ordinal"] in load_set:
            kf_path = target_dir / step["keyframeRelativePath"]
            if kf_path.exists():
                entry["keyframeJpegBase64"] = base64.b64encode(kf_path.read_bytes()).decode("ascii")
        enriched.append(entry)

    return {
        "ok": True,
        "manifest": manifest,
        "steps": enriched,
        "keyframesIncluded": include_keyframes,
        "keyframeLoadCount": len(load_set),
        "transcriptPath": str(target_dir / "transcript.md"),
    }


def delete_tutorial_playbook(name: str) -> dict[str, Any]:
    """Remove a saved tutorial playbook directory and all of its keyframes."""
    target_dir = _playbook_dir(name)
    if not target_dir.exists():
        raise TutorialPlaybookError(f"tutorial playbook '{name}' not found")
    with _lock:
        shutil.rmtree(target_dir)
    return {"ok": True, "name": name, "deleted": True, "directory": str(target_dir)}


def rename_tutorial_playbook(
    old_name: str,
    new_name: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Rename a saved playbook so its on-disk slug + recorded name reflect a memorable topic.

    Captured playbooks default to video-title-derived names that are hard to recall
    later (``jooviers-day-trading-2026-full``). Rename to a short topic label
    (``day-trading-futures``) so ``/study``, ``compose_skill_scaffold_from_playbook``,
    and ``list_tutorial_playbooks`` all surface a name users can actually type.

    Moves the directory, updates manifest.json (``name`` + ``slug``), updates
    lessons.json (``name`` + ``slug``) if it exists, and rewrites the H1 of
    lessons.md / transcript.md when those files start with the old name.
    """
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name:
        raise TutorialPlaybookError("old_name and new_name are both required")
    if old_name == new_name:
        raise TutorialPlaybookError("old_name and new_name are identical")

    src = _playbook_dir(old_name)
    dst = _playbook_dir(new_name)
    if not src.exists():
        raise TutorialPlaybookError(f"tutorial playbook '{old_name}' not found")
    if src.resolve() == dst.resolve():
        raise TutorialPlaybookError(
            f"'{old_name}' and '{new_name}' slugify to the same directory; "
            "pick a more distinct name"
        )

    new_slug = _slugify(new_name)

    with _lock:
        if dst.exists():
            if not overwrite:
                raise TutorialPlaybookError(
                    f"tutorial playbook '{new_name}' already exists; "
                    "pass overwrite=true to replace it"
                )
            shutil.rmtree(dst)
        shutil.move(str(src), str(dst))

        manifest_path = dst / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["name"] = new_name
            manifest["slug"] = new_slug
            manifest["directory"] = str(dst)
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        lessons_path = dst / "lessons.json"
        if lessons_path.exists():
            lessons = json.loads(lessons_path.read_text(encoding="utf-8"))
            lessons["name"] = new_name
            lessons["slug"] = new_slug
            lessons_path.write_text(json.dumps(lessons, indent=2), encoding="utf-8")

        for md_name in ("lessons.md", "transcript.md"):
            md_path = dst / md_name
            if not md_path.exists():
                continue
            text = md_path.read_text(encoding="utf-8")
            # Replace the first H1 only — body content stays intact.
            lines = text.split("\n", 1)
            if lines and lines[0].startswith("# "):
                # The H1 might be "# <old_name>" or "# <old_name> — distilled lessons" etc.
                # Replace just the leading name portion.
                old_h1 = lines[0]
                if old_name in old_h1:
                    new_h1 = old_h1.replace(old_name, new_name, 1)
                    md_path.write_text(
                        new_h1 + ("\n" + lines[1] if len(lines) > 1 else ""),
                        encoding="utf-8",
                    )

    return {
        "ok": True,
        "oldName": old_name,
        "newName": new_name,
        "slug": new_slug,
        "directory": str(dst),
    }


def get_tutorial_playbook_keyframe(name: str, ordinal: int) -> bytes:
    """Return the raw JPEG bytes for one keyframe inside a saved playbook."""
    target_dir = _playbook_dir(name)
    steps_path = target_dir / "steps.json"
    if not steps_path.exists():
        raise TutorialPlaybookError(f"tutorial playbook '{name}' not found")
    steps_payload = json.loads(steps_path.read_text(encoding="utf-8"))
    for step in steps_payload.get("steps") or []:
        if int(step.get("ordinal", -1)) == int(ordinal):
            kf_path = target_dir / step["keyframeRelativePath"]
            if not kf_path.exists():
                raise TutorialPlaybookError(
                    f"keyframe file missing for ordinal {ordinal} in playbook '{name}'"
                )
            return kf_path.read_bytes()
    raise TutorialPlaybookError(f"no step with ordinal {ordinal} in playbook '{name}'")
