"""Hermetic tests for the persistent tutorial playbook store."""
from __future__ import annotations

import base64
import json
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image as _PILImage

import skillmint.live_video as lv
import skillmint.tutorial_playbooks as tp


def _make_jpeg(color: tuple[int, int, int]) -> bytes:
    """Build a tiny solid-color JPEG for keyframe payloads."""
    image = _PILImage.new("RGB", (64, 48), color=color)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


SAMPLE_METADATA = {
    "id": "tut01",
    "title": "Sample Tutorial",
    "channel": "Periphery Tests",
    "is_live": False,
    "live_status": "not_live",
    "duration": 600,
    "webpage_url": "https://example.test/watch?v=tut01",
    "formats": [
        {
            "format_id": "1",
            "vcodec": "avc1",
            "acodec": "none",
            "url": "https://stream.example/v.mp4",
            "protocol": "https",
            "height": 720,
            "tbr": 1500,
            "ext": "mp4",
        }
    ],
    "subtitles": {},
    "automatic_captions": {},
}


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path: Path):
    """Point the playbook store and live-video helpers at hermetic fixtures."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path / "playbooks"))
    monkeypatch.setattr(lv, "_require_executable", lambda name: f"/fake/bin/{name}")
    monkeypatch.setattr(lv, "_start_video_extractor", lambda session: None)
    monkeypatch.setattr(lv, "_start_audio_extractor", lambda session: None)
    monkeypatch.setattr(lv, "_start_caption_poller", lambda session: None)
    monkeypatch.setattr(lv, "_start_step_watchdog", lambda session: None)
    monkeypatch.setattr(lv, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)
    yield
    lv._reset_all_sessions_for_tests()


def _populate_session_with_steps(step_count: int = 3) -> str:
    """Start a fake watch session and append step_count visually distinct frames."""
    start = lv.start_youtube_watch(
        "https://example.test/watch?v=tut01",
        min_step_seconds=0.0,
        keyframe_diff_threshold=5.0,
        include_captions=False,
        include_audio=False,
    )
    session = lv._require_session(start["sessionId"])
    palette = [(10, 10, 10), (240, 50, 50), (50, 240, 50), (50, 50, 240), (250, 250, 250)]
    for idx in range(step_count):
        color = palette[idx % len(palette)]
        session.append_transcript(f"step {idx + 1} narration", time.time() - 1, time.time())
        session.append_frame(_make_jpeg(color), 64, 48)
    return start["sessionId"]


def test_save_tutorial_writes_manifest_steps_keyframes_and_transcript() -> None:
    """A saved playbook produces manifest.json, steps.json, transcript.md, and per-step JPEGs."""
    session_id = _populate_session_with_steps(3)
    result = tp.save_tutorial_as_playbook(session_id, "Power BI Refresh", summary="Recap")
    assert result["ok"] is True
    assert result["stepCount"] == 3
    target = Path(result["directory"])
    assert (target / "manifest.json").exists()
    assert (target / "steps.json").exists()
    assert (target / "transcript.md").exists()
    assert (target / "keyframes" / "001.jpg").exists()
    assert (target / "keyframes" / "003.jpg").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["name"] == "Power BI Refresh"
    assert manifest["summary"] == "Recap"
    assert manifest["sourceUrl"] == "https://example.test/watch?v=tut01"


def test_save_tutorial_refuses_to_overwrite_without_flag() -> None:
    """Saving twice with the same name without overwrite raises TutorialPlaybookError."""
    session_id = _populate_session_with_steps(2)
    tp.save_tutorial_as_playbook(session_id, "Demo", overwrite=False)
    with pytest.raises(tp.TutorialPlaybookError, match="already exists"):
        tp.save_tutorial_as_playbook(session_id, "Demo", overwrite=False)


def test_save_tutorial_overwrite_replaces_previous_directory() -> None:
    """overwrite=True replaces the previous playbook directory cleanly."""
    session_id = _populate_session_with_steps(2)
    first = tp.save_tutorial_as_playbook(session_id, "Demo")
    second = tp.save_tutorial_as_playbook(session_id, "Demo", overwrite=True)
    assert first["directory"] == second["directory"]
    target = Path(second["directory"])
    keyframe_files = sorted((target / "keyframes").iterdir())
    assert len(keyframe_files) == 2


def test_save_tutorial_rejects_session_with_no_steps() -> None:
    """Saving a session that has emitted zero step events errors out helpfully."""
    start = lv.start_youtube_watch(
        "https://example.test/watch?v=tut01",
        include_captions=False,
        include_audio=False,
    )
    with pytest.raises(tp.TutorialPlaybookError, match="step events"):
        tp.save_tutorial_as_playbook(start["sessionId"], "Empty Tutorial")


def test_list_tutorial_playbooks_returns_persisted_summaries() -> None:
    """The list call enumerates every saved tutorial with its summary metadata."""
    session_id = _populate_session_with_steps(2)
    tp.save_tutorial_as_playbook(session_id, "Alpha")
    tp.save_tutorial_as_playbook(session_id, "Beta")
    listing = tp.list_tutorial_playbooks()
    assert listing["count"] == 2
    names = sorted(entry["name"] for entry in listing["playbooks"])
    assert names == ["Alpha", "Beta"]


def test_read_tutorial_playbook_returns_metadata_and_steps_without_bytes() -> None:
    """Default read returns text-only step records without inflating with keyframe bytes."""
    session_id = _populate_session_with_steps(3)
    tp.save_tutorial_as_playbook(session_id, "Replay Me", summary="Recap text")
    loaded = tp.read_tutorial_playbook("Replay Me")
    assert loaded["manifest"]["stepCount"] == 3
    assert loaded["manifest"]["summary"] == "Recap text"
    assert len(loaded["steps"]) == 3
    assert "keyframeJpegBase64" not in loaded["steps"][0]


def test_read_tutorial_playbook_can_include_keyframes_with_cap() -> None:
    """include_keyframes + max_keyframes only inlines the most recent N JPEGs as base64."""
    session_id = _populate_session_with_steps(4)
    tp.save_tutorial_as_playbook(session_id, "Visual Replay")
    loaded = tp.read_tutorial_playbook(
        "Visual Replay",
        include_keyframes=True,
        max_keyframes=2,
    )
    keyframe_bytes_count = sum(
        "keyframeJpegBase64" in step for step in loaded["steps"]
    )
    assert keyframe_bytes_count == 2
    # The two loaded keyframes should be the trailing ones (ordinals 3 and 4).
    trailing = [step for step in loaded["steps"] if "keyframeJpegBase64" in step]
    assert {step["ordinal"] for step in trailing} == {3, 4}
    # Sanity-check the base64 round-trips back to JPEG bytes.
    decoded = base64.b64decode(trailing[0]["keyframeJpegBase64"])
    assert decoded[:2] == b"\xff\xd8"


def test_read_tutorial_playbook_unknown_name_raises() -> None:
    """Reading a non-existent playbook raises TutorialPlaybookError."""
    with pytest.raises(tp.TutorialPlaybookError, match="not found"):
        tp.read_tutorial_playbook("does-not-exist")


def test_delete_tutorial_playbook_removes_directory() -> None:
    """Deleting a saved playbook removes its on-disk directory."""
    session_id = _populate_session_with_steps(2)
    saved = tp.save_tutorial_as_playbook(session_id, "Disposable")
    target = Path(saved["directory"])
    assert target.exists()
    tp.delete_tutorial_playbook("Disposable")
    assert not target.exists()


def test_get_tutorial_playbook_keyframe_returns_jpeg_bytes() -> None:
    """Fetching one keyframe by ordinal returns the raw JPEG payload from disk."""
    session_id = _populate_session_with_steps(3)
    tp.save_tutorial_as_playbook(session_id, "Frame Picker")
    jpeg = tp.get_tutorial_playbook_keyframe("Frame Picker", 2)
    assert jpeg[:2] == b"\xff\xd8"
    assert jpeg[-2:] == b"\xff\xd9"


def test_slugify_normalizes_names_for_filesystem() -> None:
    """_slugify maps unsafe characters to dashes and trims length."""
    assert tp._slugify("Power BI / Refresh!") == "power-bi-refresh"
    assert tp._slugify("a" * 200).startswith("aaaa")
    assert len(tp._slugify("a" * 200)) <= 80


# Distill -------------------------------------------------------------------


def test_clean_caption_text_strips_karaoke_tags() -> None:
    """Inline VTT timing and color tags must be stripped, leaving only the text."""
    raw = "By<00:00:00.120><c> the</c><00:00:00.200><c> end</c><00:00:00.360><c> of</c>"
    assert tp._clean_caption_text(raw) == "By the end of"


def test_clean_caption_text_collapses_rolling_overlap() -> None:
    """Per-step rolling-window duplicates must collapse via word-boundary overlap merge."""
    raw = (
        "By the end of this video, you're going\n"
        "By the end of this video, you're going to know everything you need to know to\n"
        "to know everything you need to know to become a profitable day trader"
    )
    out = tp._clean_caption_text(raw)
    # The rolling tail "to know everything you need to know to" must appear exactly once.
    assert out.count("to know everything") == 1
    assert "become a profitable day trader" in out


def test_dedupe_section_text_merges_cross_step_overlap() -> None:
    """Across steps, when a later fragment starts with the previous fragment's tail, merge cleanly."""
    parts = [
        "By the end of this video, you're going to know everything you need to know to",
        "to know everything you need to know to become a profitable day trader, even if",
        "become a profitable day trader, even if you're starting today as a complete",
    ]
    out = tp._dedupe_section_text(parts)
    # Each rolling chunk should appear once, not three times.
    assert out.count("to know everything") == 1
    assert out.count("profitable day trader") == 1
    assert "starting today as a complete" in out


def test_build_sections_groups_steps_by_quiet_and_diff() -> None:
    """A quiet_period step or a step with high diffScore must start a new section."""
    steps = [
        {"ordinal": 1, "trigger": "keyframe", "diffScore": None,
         "videoStartSeconds": 0.0, "videoEndSeconds": 5.0,
         "captionText": "Intro talk", "keyframeRelativePath": "keyframes/001.jpg"},
        {"ordinal": 2, "trigger": "keyframe", "diffScore": 15.0,
         "videoStartSeconds": 5.0, "videoEndSeconds": 10.0,
         "captionText": "Intro continues", "keyframeRelativePath": "keyframes/002.jpg"},
        {"ordinal": 3, "trigger": "quiet_period", "diffScore": 0.0,
         "videoStartSeconds": 10.0, "videoEndSeconds": 20.0,
         "captionText": "New topic after pause", "keyframeRelativePath": "keyframes/003.jpg"},
        {"ordinal": 4, "trigger": "keyframe", "diffScore": 90.0,
         "videoStartSeconds": 20.0, "videoEndSeconds": 25.0,
         "captionText": "Hard cut to chart", "keyframeRelativePath": "keyframes/004.jpg"},
    ]
    sections = tp._build_sections_from_steps(steps, section_diff_score=60.0)
    assert len(sections) == 3  # steps 1+2 group, step 3 breaks (quiet), step 4 breaks (hard cut)
    assert sections[0]["stepOrdinals"] == [1, 2]
    assert sections[1]["stepOrdinals"] == [3]
    assert sections[2]["stepOrdinals"] == [4]
    assert sections[2]["anchorKeyframePath"] == "keyframes/004.jpg"


def test_build_sections_carries_visual_actions() -> None:
    steps = [
        {
            "ordinal": 1,
            "trigger": "keyframe",
            "diffScore": None,
            "videoStartSeconds": 0.0,
            "videoEndSeconds": 1.0,
            "captionText": "Open settings",
            "keyframeRelativePath": "keyframes/001.jpg",
            "visualAction": {
                "actionType": "navigation_or_tool_change",
                "confidence": 0.72,
                "changedRatio": 0.1,
                "changedRegion": {"x": 0, "y": 0, "width": 20, "height": 20},
                "changedZones": ["top_bar"],
                "ocr": {"visibleTextSample": "Settings"},
                "observations": ["navigation or tool chrome changed"],
            },
        }
    ]

    sections = tp._build_sections_from_steps(steps)

    assert sections[0]["visualActions"][0]["actionType"] == "navigation_or_tool_change"
    assert sections[0]["visualActions"][0]["visibleTextSample"] == "Settings"


def test_distill_tutorial_playbook_writes_lessons_files(tmp_path, monkeypatch) -> None:
    """End-to-end: a saved playbook gets a lessons.md + lessons.json after distill."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    session_id = _populate_session_with_steps(4)
    tp.save_tutorial_as_playbook(session_id, "distill-target", overwrite=True)
    result = tp.distill_tutorial_playbook("distill-target")
    assert result["ok"] is True
    assert result["sectionCount"] >= 1
    target_dir = tmp_path / "distill-target"
    assert (target_dir / "lessons.md").exists()
    assert (target_dir / "lessons.json").exists()
    payload = json.loads((target_dir / "lessons.json").read_text(encoding="utf-8"))
    assert payload["sectionCount"] == result["sectionCount"]
    assert len(payload["sections"]) == result["sectionCount"]
    assert "visualActions" in payload["sections"][0]


def test_distill_tutorial_playbook_unknown_name_raises() -> None:
    """Asking to distill a playbook that doesn't exist surfaces a TutorialPlaybookError."""
    with pytest.raises(tp.TutorialPlaybookError, match="not found"):
        tp.distill_tutorial_playbook("there-is-no-such-playbook-9c2e")


# Rename --------------------------------------------------------------------


def test_rename_moves_directory_and_updates_manifest(tmp_path, monkeypatch) -> None:
    """A successful rename relocates the dir and updates name/slug in manifest + lessons."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    session_id = _populate_session_with_steps(3)
    tp.save_tutorial_as_playbook(session_id, "verbose-original-name")
    tp.distill_tutorial_playbook("verbose-original-name")
    result = tp.rename_tutorial_playbook("verbose-original-name", "short-name")
    assert result["ok"] is True
    assert not (tmp_path / "verbose-original-name").exists()
    new_dir = tmp_path / "short-name"
    assert new_dir.exists()
    manifest = json.loads((new_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "short-name"
    assert manifest["slug"] == "short-name"
    lessons = json.loads((new_dir / "lessons.json").read_text(encoding="utf-8"))
    assert lessons["name"] == "short-name"


def test_rename_rejects_unknown_source(tmp_path, monkeypatch) -> None:
    """Renaming a nonexistent playbook raises."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    with pytest.raises(tp.TutorialPlaybookError, match="not found"):
        tp.rename_tutorial_playbook("ghost", "anything")


def test_rename_refuses_to_overwrite_without_flag(tmp_path, monkeypatch) -> None:
    """Renaming into an existing slug fails unless overwrite=True."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    s1 = _populate_session_with_steps(2)
    s2 = _populate_session_with_steps(2)
    tp.save_tutorial_as_playbook(s1, "source-name")
    tp.save_tutorial_as_playbook(s2, "occupied-name")
    with pytest.raises(tp.TutorialPlaybookError, match="already exists"):
        tp.rename_tutorial_playbook("source-name", "occupied-name")
    # With overwrite=True, the rename succeeds and the occupied dir is replaced.
    result = tp.rename_tutorial_playbook("source-name", "occupied-name", overwrite=True)
    assert result["ok"] is True
    assert not (tmp_path / "source-name").exists()


def test_rename_rejects_self_rename(tmp_path, monkeypatch) -> None:
    """Renaming a playbook to its own name is rejected up front."""
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))
    sid = _populate_session_with_steps(2)
    tp.save_tutorial_as_playbook(sid, "self")
    with pytest.raises(tp.TutorialPlaybookError, match="identical"):
        tp.rename_tutorial_playbook("self", "self")
