"""Tests for skillmint.offline_video_capture (batch VOD download + decode-speed processing).

These tests mock the network/IO boundary (yt-dlp download + ffmpeg subprocess + caption HTTP)
so they run offline. The keyframe diff and caption-window logic are exercised against
synthetic JPEG bytes and a fake ffmpeg stdout stream.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
from typing import Any

import pytest

from skillmint import offline_video_capture as ovc
from skillmint import live_video, tutorial_playbooks


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
    assert steps[0]["visualAction"]["actionType"] == "initial_view"
    assert steps[1]["visualAction"]["actionType"] in {"screen_transition", "layout_change"}
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


def test_process_local_video_flushes_trailing_captions_after_last_keyframe(
    monkeypatch, tmp_path
) -> None:
    """Regression: dialogue after the last visual scene-change must not be dropped.

    The keyframe loop only appends a step when the picture changes enough to
    cross the diff threshold. If the picture goes static for the remainder of
    the recording (e.g. a screen-share stops updating while people keep
    talking, very common in a meeting recording) there is no further keyframe
    to anchor a step to, so any captions/transcript after the last keyframe
    used to be silently dropped from the playbook and every downstream lesson.
    """
    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    blue = _make_jpeg((0, 0, 255))
    # Frames at 0s,1s,2s,3s,4s: red/green/blue each trigger a keyframe (t=0,1,2),
    # then two more blue frames (t=3,4) that do NOT trigger a new keyframe.
    payload = red + green + blue + blue + blue
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(payload))
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")

    cues = {
        "en": [
            {"startSeconds": 0.5, "endSeconds": 1.0, "text": "early remark"},
            # Falls after the last keyframe's step end (t=2.0) — this is the
            # part that used to vanish with no trailing step to hold it.
            {"startSeconds": 3.2, "endSeconds": 3.8, "text": "closing decision"},
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

    # 3 real keyframe steps (red, green, blue) plus one trailing quiet-period flush.
    assert len(steps) == 4
    trailing = steps[-1]
    assert trailing["trigger"] == "quiet_period"
    assert trailing["videoStartSeconds"] == 2.0
    assert trailing["videoEndSeconds"] >= 3.8
    assert "closing decision" in trailing["captionText"]
    # No caption text is dropped anywhere: every cue must show up in exactly one step.
    all_caption_text = " ".join(s["captionText"] for s in steps)
    assert "early remark" in all_caption_text
    assert "closing decision" in all_caption_text


def test_process_local_video_skips_empty_trailing_flush(monkeypatch, tmp_path) -> None:
    """No trailing step should be added when there's nothing left to say — no step spam."""
    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    payload = red + green + green  # green repeats with no further scene change
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(payload))
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")

    steps = ovc._process_local_video(
        str(tmp_path / "fake.mp4"),
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        caption_cues_by_lang={},  # nothing was ever said
        timeout_seconds=30.0,
    )

    assert len(steps) == 2
    assert all(s["trigger"] != "quiet_period" for s in steps)


def test_capture_youtube_video_rejects_live_streams(monkeypatch) -> None:
    """The offline path must reject live streams and steer callers to start_youtube_watch."""
    live_meta = dict(SAMPLE_METADATA)
    live_meta["is_live"] = True
    monkeypatch.setattr(ovc, "_run_ytdlp_metadata", lambda url: live_meta)
    with pytest.raises(live_video.LiveVideoError, match="VODs only"):
        ovc.capture_youtube_video_to_playbook(
            "https://youtu.be/abc123", "fake-name", overwrite=True
        )


def test_capture_local_video_rejects_missing_path(tmp_path) -> None:
    """Missing file path must surface as a LiveVideoError, not a bare OSError."""
    with pytest.raises(live_video.LiveVideoError, match="not found"):
        ovc.capture_local_video_to_playbook(
            str(tmp_path / "nope.mp4"), "fake-name", overwrite=True
        )


def test_capture_local_video_rejects_missing_captions(monkeypatch, tmp_path) -> None:
    """Captions path mismatch must error early, before any ffmpeg work runs."""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-mp4-bytes")
    with pytest.raises(live_video.LiveVideoError, match="captions_path"):
        ovc.capture_local_video_to_playbook(
            str(video_path),
            "fake-name",
            captions_path=str(tmp_path / "missing.vtt"),
            overwrite=True,
        )


def test_capture_local_video_end_to_end_persists_playbook(monkeypatch, tmp_path) -> None:
    """Local-file path: skips yt-dlp, decodes via ffmpeg, persists playbook with sidecar captions."""
    video_path = tmp_path / "local.mp4"
    video_path.write_bytes(b"fake-mp4-bytes")
    captions_path = tmp_path / "local.vtt"
    captions_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.500 --> 00:00:01.000\nopening line\n\n"
        "00:00:02.500 --> 00:00:03.000\nsecond line\n",
        encoding="utf-8",
    )

    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    blue = _make_jpeg((0, 0, 255))
    payload = red + green + blue
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(payload))
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")

    playbook_root = tmp_path / "playbooks"
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(playbook_root))

    result = ovc.capture_local_video_to_playbook(
        str(video_path),
        name="local-test",
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        captions_path=str(captions_path),
        overwrite=True,
        summary="local unit test",
    )
    assert result["ok"] is True
    assert result["stepCount"] >= 2
    assert result["captionErrors"] == {}
    playbook_dir = playbook_root / "local-test"
    assert (playbook_dir / "manifest.json").exists()
    assert (playbook_dir / "steps.json").exists()
    assert (playbook_dir / "transcript.md").exists()
    keyframes = sorted((playbook_dir / "keyframes").iterdir())
    assert len(keyframes) == result["stepCount"]
    steps_payload = json.loads((playbook_dir / "steps.json").read_text(encoding="utf-8"))
    assert steps_payload["steps"][0]["visualAction"]["schema"] == "skillmint.visual_action.v1"


def test_capture_local_video_works_without_captions(monkeypatch, tmp_path) -> None:
    """transcribe=False with no captions_path = pure keyframes capture, no caption text."""
    video_path = tmp_path / "silent.mp4"
    video_path.write_bytes(b"fake-mp4-bytes")

    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    monkeypatch.setattr(ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(red + green))
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")

    playbook_root = tmp_path / "playbooks"
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(playbook_root))

    result = ovc.capture_local_video_to_playbook(
        str(video_path),
        name="silent-test",
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        transcribe=False,
        overwrite=True,
    )
    assert result["ok"] is True
    assert result["stepCount"] >= 1
    assert result["whisper"] is None


def test_capture_local_video_auto_transcribes_when_no_sidecar(monkeypatch, tmp_path) -> None:
    """transcribe=True with no captions_path triggers _transcribe_audio_to_cues; cues land on steps."""
    video_path = tmp_path / "talking.mp4"
    video_path.write_bytes(b"fake-mp4-bytes")

    fake_cues = [
        {"startSeconds": 0.0, "endSeconds": 1.0, "text": "welcome to sql"},
        {"startSeconds": 1.0, "endSeconds": 2.0, "text": "this is a select statement"},
        {"startSeconds": 2.0, "endSeconds": 3.0, "text": "now we add a where clause"},
    ]
    fake_meta = {
        "model": "base",
        "device": "cuda",
        "computeType": "float16",
        "detectedLanguage": "en",
        "languageProbability": 0.99,
        "audioDurationSeconds": 3.0,
        "cueCount": 3,
    }

    def fake_transcribe(path, *, model_name, device, language):
        assert path == str(video_path)
        return fake_cues, fake_meta

    monkeypatch.setattr(ovc, "_transcribe_audio_to_cues", fake_transcribe)

    red = _make_jpeg((255, 0, 0))
    green = _make_jpeg((0, 255, 0))
    blue = _make_jpeg((0, 0, 255))
    monkeypatch.setattr(
        ovc.subprocess, "Popen", lambda *a, **kw: _FakeProc(red + green + blue)
    )
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")

    playbook_root = tmp_path / "playbooks"
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(playbook_root))

    result = ovc.capture_local_video_to_playbook(
        str(video_path),
        name="transcribe-test",
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        overwrite=True,
    )
    assert result["ok"] is True
    assert result["whisper"] == fake_meta
    transcript = (playbook_root / "transcribe-test" / "transcript.md").read_text()
    assert "welcome to sql" in transcript or "select statement" in transcript


def test_capture_local_video_transcribe_failure_is_soft(monkeypatch, tmp_path) -> None:
    """A whisper failure must not abort capture; it records to captionErrors and continues."""
    video_path = tmp_path / "noisy.mp4"
    video_path.write_bytes(b"fake-mp4-bytes")

    def fake_transcribe_raises(path, *, model_name, device, language):
        raise live_video.LiveVideoError("model OOM")

    monkeypatch.setattr(ovc, "_transcribe_audio_to_cues", fake_transcribe_raises)
    monkeypatch.setattr(
        ovc.subprocess, "Popen",
        lambda *a, **kw: _FakeProc(_make_jpeg((10, 10, 10)) + _make_jpeg((250, 10, 10))),
    )
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path / "playbooks"))

    result = ovc.capture_local_video_to_playbook(
        str(video_path),
        name="soft-fail-test",
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        overwrite=True,
    )
    assert result["ok"] is True
    assert "transcribe failed" in result["captionErrors"]["en"]


def test_resolve_whisper_device_explicit_passthrough() -> None:
    """Explicit 'cuda' or 'cpu' passes through without probing; bad input errors."""
    assert ovc._resolve_whisper_device("cpu") == ("cpu", "int8")
    assert ovc._resolve_whisper_device("cuda") == ("cuda", "float16")
    with pytest.raises(live_video.LiveVideoError, match="whisper_device"):
        ovc._resolve_whisper_device("tpu")


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
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))

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


def test_capture_youtube_video_transcribes_when_captions_missing(monkeypatch, tmp_path) -> None:
    """YouTube VOD capture should fall back to faster-whisper when caption tracks are absent."""
    metadata = dict(SAMPLE_METADATA)
    metadata["automatic_captions"] = {}
    monkeypatch.setattr(ovc, "_run_ytdlp_metadata", lambda url: metadata)
    monkeypatch.setattr(ovc, "_select_caption_tracks", lambda metadata, languages: {})

    def fake_download(url, tmpdir, *, max_height, timeout_seconds):
        path = os.path.join(tmpdir, "video.mp4")
        with open(path, "wb") as fh:
            fh.write(b"fake-mp4-bytes")
        return path

    fake_meta = {
        "model": "base",
        "device": "cpu",
        "computeType": "int8",
        "detectedLanguage": "en",
        "languageProbability": 0.9,
        "audioDurationSeconds": 2.0,
        "cueCount": 2,
    }

    def fake_transcribe(path, *, model_name, device, language):
        assert model_name == "base"
        assert device == "auto"
        assert language == "en"
        return [
            {"startSeconds": 0.0, "endSeconds": 0.5, "text": "fallback transcript"},
            {"startSeconds": 1.0, "endSeconds": 1.5, "text": "source lesson"},
        ], fake_meta

    monkeypatch.setattr(ovc, "_download_with_ytdlp", fake_download)
    monkeypatch.setattr(ovc, "_transcribe_audio_to_cues", fake_transcribe)
    monkeypatch.setattr(
        ovc.subprocess,
        "Popen",
        lambda *a, **kw: _FakeProc(
            _make_jpeg((255, 0, 0)) + _make_jpeg((0, 255, 0))
        ),
    )
    monkeypatch.setattr(ovc, "_require_executable", lambda name: "/fake/ffmpeg")
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path))

    result = ovc.capture_youtube_video_to_playbook(
        "https://youtu.be/abc123",
        name="youtube-transcribe-test",
        fps=1.0,
        frame_width=64,
        keyframe_diff_threshold=5.0,
        min_step_seconds=0.5,
        overwrite=True,
    )

    assert result["ok"] is True
    assert result["whisper"] == fake_meta
    transcript = (tmp_path / "youtube-transcribe-test" / "transcript.md").read_text(
        encoding="utf-8"
    )
    assert "fallback transcript" in transcript or "source lesson" in transcript
