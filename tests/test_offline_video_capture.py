"""Tests for periscribe.offline_video_capture (batch VOD download + decode-speed processing).

These tests mock the network/IO boundary (yt-dlp download + ffmpeg subprocess + caption HTTP)
so they run offline. The keyframe diff and caption-window logic are exercised against
synthetic JPEG bytes and a fake ffmpeg stdout stream.
"""
from __future__ import annotations

import io
import os
import subprocess
from typing import Any

import pytest

from periscribe import offline_video_capture as ovc
from periscribe import live_video, tutorial_playbooks


SAMPLE_METADATA = {
    "id": "abc123",
    "title": "Mock VOD",
    "uploader": "tester",
    "channel": "tester",
    "is_live": False,
    "duration": 300,
    "view_count": 1,
    "upload_date": "20260101",
    "webpage_url": "https://youtu.be/abc123",
    "thumbnail": "https://i.ytimg.com/vi/abc123/default.jpg",
    "automatic_captions": {"en": [{"ext": "vtt", "url": "https://example/captions.vtt"}]},
    "formats": [
        {"vcodec": "avc1.0", "acodec": "none", "url": "https://example/video.m3u8",
         "ext": "m3u8", "height": 480, "width": 854, "tbr": 1000, "protocol": "m3u8_native"}
    ],
}


def _make_jpeg(color: tuple[int, int, int], size: tuple[int, int] = (64, 48)) -> bytes:
    """Build a small solid-color JPEG so the diff path has something to chew on."""
    from io import BytesIO
    from PIL import Image
    image = Image.new("RGB", size, color)
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


class _FakeProc:
    """Stand-in for subprocess.Popen that yields a fixed JPEG byte stream."""
    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(b"")
        self.returncode = 0
        self._terminated = False

    def terminate(self) -> None:
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self._terminated = True


def test_captions_in_window_picks_cues_by_timestamp() -> None:
    """Cues fall into a step's window when their startSeconds is within [start, end)."""
    cues = {
        "en": [
            {"startSeconds": 0.5, "endSeconds": 1.0, "text": "alpha"},
            {"startSeconds": 2.0, "endSeconds": 2.5, "text": "beta"},
            {"startSeconds": 5.0, "endSeconds": 5.5, "text": "gamma"},
        ]
    }
    assert ovc._captions_in_window(cues, 0.0, 3.0) == "alpha\nbeta"
    assert ovc._captions_in_window(cues, 3.0, 6.0) == "gamma"
    assert ovc._captions_in_window(cues, 6.0, 9.0) == ""


def test_captions_in_window_skips_untimed_cues() -> None:
    """A cue without a numeric startSeconds is silently dropped (parser leak guard)."""
    cues = {"en": [{"startSeconds": None, "endSeconds": None, "text": "header leak"}]}
    assert ovc._captions_in_window(cues, 0.0, 100.0) == ""


def test_process_local_video_emits_step_per_keyframe(monkeypatch, tmp_path) -> None:
    """Distinct-colored JPEGs should each cross the diff threshold and yield a step."""
    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    blue = _make_jpeg((0, 0, 255))
    fake_payload = red + red + green + blue
    fake_proc = _FakeProc(fake_payload)
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: fake_proc)
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")

    cues = {
        "en": [
            {"startSeconds": 0.5, "endSeconds": 1.0, "text": "first"},
            {"startSeconds": 1.5, "endSeconds": 2.0, "text": "second"},
            {"startSeconds": 2.5, "endSeconds": 3.0, "text": "third"},
        ]
    }
    steps = ovc._process_local_video(
        str(tmp_path / "fake.mp4"),
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        caption_cues_by_lang=cues,
        timeout_seconds=30.0,
    )
    # 4 frames in: red (always-keyframe seed), red (no diff), green (diff), blue (diff)
    assert len(steps) == 3
    assert [s["sequence"] for s in steps] == [1, 2, 3]
    assert steps[0]["videoStartSeconds"] == 0.0
    # Step 1 covers [0.0, 0.0) — no cues yet (seed frame); steps 2/3 should pick up cues.
    assert "second" in steps[1]["captionText"] or "first" in steps[1]["captionText"]


def test_process_local_video_respects_min_step_seconds(monkeypatch, tmp_path) -> None:
    """A keyframe that arrives too soon after the previous one must be suppressed."""
    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    blue = _make_jpeg((0, 0, 255))
    payload = red + green + blue + green
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(payload))
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")
    steps = ovc._process_local_video(
        str(tmp_path / "fake.mp4"),
        fps=2.0,  # frames at 0s, 0.5s, 1.0s, 1.5s
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=2.0,  # any second keyframe within 2s is suppressed
        caption_cues_by_lang={},
        timeout_seconds=30.0,
    )
    # Only the seed keyframe survives because all subsequent diffs fall inside min_step_seconds.
    assert len(steps) == 1


def test_capture_youtube_video_rejects_live_streams(monkeypatch) -> None:
    """The offline path must reject live streams and steer callers to start_youtube_watch."""
    live_meta = dict(SAMPLE_METADATA)
    live_meta["is_live"] = True
    monkeypatch.setattr(ovc, "_run_ytdlp_metadata", lambda url: live_meta)
    with pytest.raises(live_video.LiveVideoError, match="VODs only"):
        ovc.capture_youtube_video_to_playbook(
            "https://youtu.be/abc123", "fake-name", overwrite=True
        )


def test_capture_youtube_video_end_to_end_persists_playbook(monkeypatch, tmp_path) -> None:
    """Glue test: stub network + ffmpeg, run the whole pipeline, verify a playbook lands on disk."""
    monkeypatch.setattr(ovc, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)

    def fake_select_caption_tracks(metadata, languages):
        return {"en": "https://example/captions.vtt"}

    monkeypatch.setattr(ovc, "_select_caption_tracks", fake_select_caption_tracks)
    monkeypatch.setattr(
        ovc,
        "_http_get_text",
        lambda url, *, timeout_seconds: (
            "WEBVTT\n\n"
            "00:00:00.500 --> 00:00:01.000\nopening line\n\n"
            "00:00:02.500 --> 00:00:03.000\nsecond line\n"
        ),
    )

    def fake_download(url, tmpdir, *, max_height, timeout_seconds):
        path = os.path.join(tmpdir, "video.mp4")
        with open(path, "wb") as fh:
            fh.write(b"fake-mp4-bytes")
        return path

    monkeypatch.setattr(ovc, "_download_with_ytdlp", fake_download)

    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    blue = _make_jpeg((0, 0, 255))
    payload = red + green + blue
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(payload))
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")
    monkeypatch.setenv("PERISCRIBE_PLAYBOOK_DIR", str(tmp_path))

    result = ovc.capture_youtube_video_to_playbook(
        "https://youtu.be/abc123",
        name="offline-test",
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        overwrite=True,
        summary="unit test",
    )
    assert result["ok"] is True
    assert result["stepCount"] >= 2
    playbook_dir = tmp_path / "offline-test"
    assert (playbook_dir / "manifest.json").exists()
    assert (playbook_dir / "steps.json").exists()
    assert (playbook_dir / "transcript.md").exists()
    keyframes = sorted((playbook_dir / "keyframes").iterdir())
    assert len(keyframes) == result["stepCount"]
