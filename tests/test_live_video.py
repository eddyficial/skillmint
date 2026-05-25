"""Hermetic tests for the Periphery live video lane (no network or ffmpeg required)."""
from __future__ import annotations

import json
import threading
import time

import pytest

import skillmint.live_video as live_video


# Test helpers ---------------------------------------------------------------


SAMPLE_METADATA = {
    "id": "abc123",
    "title": "Test Video",
    "uploader": "Test Channel",
    "channel": "Test Channel",
    "is_live": True,
    "live_status": "is_live",
    "duration": None,
    "view_count": 42,
    "concurrent_view_count": 7,
    "upload_date": "20260101",
    "webpage_url": "https://youtube.com/watch?v=abc123",
    "thumbnail": "https://i.example/thumb.jpg",
    "formats": [
        {
            "format_id": "299",
            "vcodec": "avc1.640028",
            "acodec": "none",
            "url": "https://stream.example/video.m3u8",
            "protocol": "m3u8_native",
            "height": 1080,
            "tbr": 5000,
            "ext": "mp4",
        },
        {
            "format_id": "140",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "url": "https://stream.example/audio.m4a",
            "abr": 128,
            "ext": "m4a",
        },
    ],
    "subtitles": {},
    "automatic_captions": {
        "en": [
            {"ext": "vtt", "url": "https://captions.example/en.vtt"},
            {"ext": "srv3", "url": "https://captions.example/en.srv3"},
        ],
        "es": [
            {"ext": "vtt", "url": "https://captions.example/es.vtt"},
        ],
    },
}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Wipe live video session state and stub executable discovery for every test."""
    monkeypatch.setattr(live_video, "_require_executable", lambda name: f"/fake/bin/{name}")
    yield
    live_video._reset_all_sessions_for_tests()


# Metadata + selection -------------------------------------------------------


def test_summarize_metadata_picks_compact_fields() -> None:
    """The compact summary keeps the agent-facing fields and drops the rest."""
    summary = live_video._summarize_metadata(SAMPLE_METADATA)
    assert summary["id"] == "abc123"
    assert summary["isLive"] is True
    assert summary["liveStatus"] == "is_live"
    assert summary["availableCaptionLanguages"] == ["en", "es"]
    assert summary["hasAutomaticCaptions"] is True


def test_select_video_format_prefers_m3u8_at_highest_height() -> None:
    """Video format selection prefers m3u8 streams at the highest available resolution."""
    metadata = {
        "formats": [
            {"vcodec": "avc1", "acodec": "none", "url": "u1", "protocol": "https", "height": 1080, "tbr": 4000},
            {"vcodec": "avc1", "acodec": "none", "url": "u2", "protocol": "m3u8_native", "height": 720, "tbr": 2000},
            {"vcodec": "avc1", "acodec": "none", "url": "u3", "protocol": "m3u8_native", "height": 1080, "tbr": 5000},
        ]
    }
    chosen = live_video._select_video_format(metadata)
    assert chosen is not None
    assert chosen["url"] == "u3"


def test_select_video_format_low_latency_prefers_smallest() -> None:
    """In low-latency mode, smaller resolutions win because smaller segments decode faster."""
    metadata = {
        "formats": [
            {"vcodec": "avc1", "acodec": "none", "url": "u1080", "protocol": "m3u8_native", "height": 1080, "tbr": 5000},
            {"vcodec": "avc1", "acodec": "none", "url": "u720", "protocol": "m3u8_native", "height": 720, "tbr": 2500},
            {"vcodec": "avc1", "acodec": "none", "url": "u360", "protocol": "m3u8_native", "height": 360, "tbr": 800},
        ]
    }
    chosen = live_video._select_video_format(metadata, low_latency=True)
    assert chosen is not None
    assert chosen["url"] == "u360"


def test_select_video_format_falls_back_to_top_level_url() -> None:
    """If no per-format URL is present, fall back to the top-level url field."""
    metadata = {"url": "https://stream.example/single.mp4", "format_id": "best"}
    chosen = live_video._select_video_format(metadata)
    assert chosen == {"url": "https://stream.example/single.mp4", "format_id": "best"}


def test_select_audio_format_picks_highest_abr() -> None:
    """Audio format selection picks the audio-only stream with the highest bitrate."""
    metadata = {
        "formats": [
            {"vcodec": "none", "acodec": "mp4a", "url": "u1", "abr": 64},
            {"vcodec": "none", "acodec": "mp4a", "url": "u2", "abr": 192},
            {"vcodec": "none", "acodec": "mp4a", "url": "u3", "abr": 128},
        ]
    }
    chosen = live_video._select_audio_format(metadata)
    assert chosen is not None
    assert chosen["url"] == "u2"


def test_select_caption_tracks_prefers_vtt() -> None:
    """Caption track selection prefers VTT over srv3/json3."""
    tracks = live_video._select_caption_tracks(SAMPLE_METADATA, ("en", "fr"))
    assert tracks["en"] == "https://captions.example/en.vtt"
    assert "fr" not in tracks


# Caption parsing ------------------------------------------------------------


def test_parse_caption_text_strips_vtt_metadata() -> None:
    """VTT parsing drops cue timing rows and header noise."""
    body = (
        "WEBVTT\n"
        "\n"
        "1\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "Hello world\n"
        "\n"
        "2\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "Second line\n"
    )
    assert live_video._parse_caption_text(body) == "Hello world\nSecond line"


def test_parse_caption_text_handles_json3() -> None:
    """YouTube json3 caption blobs parse into concatenated utf8 segments."""
    body = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "Hi "}, {"utf8": "there"}]},
                {"segs": [{"utf8": "again"}]},
            ]
        }
    )
    assert live_video._parse_caption_text(body) == "Hi there\nagain"


# Session lifecycle (with stubbed subprocess + worker threads) --------------


class _DummyProcess:
    """A no-op stand-in for subprocess.Popen for the session lifecycle tests."""

    def __init__(self) -> None:
        self.stdout = None
        self._terminated = False

    def poll(self) -> int | None:
        """Return process exit status (None means still running for our purposes)."""
        return None if not self._terminated else 0

    def terminate(self) -> None:
        """Record that termination was requested."""
        self._terminated = True

    def wait(self, timeout: float | None = None) -> int:
        """Behave as if the process exited immediately on wait."""
        self._terminated = True
        return 0


def _stub_session_workers(monkeypatch) -> dict[str, _DummyProcess]:
    """Replace the live video worker entry points with no-op subprocess stubs."""
    spawned: dict[str, _DummyProcess] = {}

    def fake_video_extractor(session: live_video._Session) -> None:
        """Attach a dummy video process without actually spawning ffmpeg."""
        proc = _DummyProcess()
        session.video_process = proc
        spawned["video"] = proc

    def fake_audio_extractor(session: live_video._Session) -> None:
        """Attach a dummy audio process only when the session asked for audio."""
        if not session.config.include_audio:
            return
        proc = _DummyProcess()
        session.audio_process = proc
        spawned["audio"] = proc

    def fake_caption_poller(session: live_video._Session) -> None:
        """Skip the caption poller thread entirely so tests stay synchronous."""
        return None

    monkeypatch.setattr(live_video, "_start_video_extractor", fake_video_extractor)
    monkeypatch.setattr(live_video, "_start_audio_extractor", fake_audio_extractor)
    monkeypatch.setattr(live_video, "_start_caption_poller", fake_caption_poller)
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)
    return spawned


def test_start_youtube_watch_returns_session_metadata(monkeypatch) -> None:
    """start_youtube_watch returns a sessionId and surfaces resolved caption languages."""
    _stub_session_workers(monkeypatch)
    result = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        include_audio=False,
        include_captions=True,
        caption_languages=("en",),
    )
    assert result["sessionId"]
    assert result["captionLanguagesResolved"] == ["en"]
    assert result["video"]["isLive"] is True
    assert result["frameCount"] == 0
    # Live URLs auto-enable low-latency mode and expose it through the config block.
    assert result["config"]["lowLatency"] is True
    assert result["config"]["isLive"] is True


def test_start_youtube_watch_threads_caption_poll_seconds(monkeypatch) -> None:
    """caption_poll_seconds clamps to its allowed range and reaches the session config."""
    _stub_session_workers(monkeypatch)
    result = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        caption_poll_seconds=0.2,  # Below MIN_CAPTION_POLL_SECONDS; should clamp up.
    )
    assert result["config"]["captionPollSeconds"] == live_video.MIN_CAPTION_POLL_SECONDS


def test_start_youtube_watch_respects_explicit_low_latency_override(monkeypatch) -> None:
    """Callers can force low_latency=False even on live streams when they want quality."""
    _stub_session_workers(monkeypatch)
    result = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        low_latency=False,
    )
    assert result["config"]["lowLatency"] is False
    assert result["config"]["isLive"] is True


def test_poll_youtube_watch_returns_only_new_records(monkeypatch) -> None:
    """Polling returns only records with sequence > since_*."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        include_audio=False,
        include_captions=False,
    )
    session_id = start["sessionId"]
    session = live_video._require_session(session_id)
    session.append_frame(b"\xff\xd8jpeg-bytes-1\xff\xd9", 640, 360)
    session.append_frame(b"\xff\xd8jpeg-bytes-2\xff\xd9", 640, 360)
    session.append_transcript("hello world", time.time() - 5, time.time())

    first = live_video.poll_youtube_watch(session_id, max_frames=10)
    assert len(first["frames"]) == 2
    assert first["frames"][0]["sequence"] == 1
    assert "jpegBase64" in first["frames"][0]
    assert len(first["transcripts"]) == 1

    last_seq = first["frames"][-1]["sequence"]
    last_tx = first["transcripts"][-1]["sequence"]
    second = live_video.poll_youtube_watch(
        session_id,
        since_frame_sequence=last_seq,
        since_transcript_sequence=last_tx,
    )
    assert second["frames"] == []
    assert second["transcripts"] == []


def test_poll_youtube_watch_wait_returns_on_new_frame(monkeypatch) -> None:
    """Long-poll wakes immediately when a new frame is appended mid-wait."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch("https://youtu.be/abc123")
    session_id = start["sessionId"]
    session = live_video._require_session(session_id)

    def push_frame_after_delay() -> None:
        """Append one frame after a short delay so the polling thread wakes up."""
        time.sleep(0.15)
        session.append_frame(b"\xff\xd8jpeg-late\xff\xd9", 320, 180)

    threading.Thread(target=push_frame_after_delay, daemon=True).start()
    started = time.time()
    result = live_video.poll_youtube_watch(session_id, wait_seconds=2.0)
    elapsed = time.time() - started
    assert len(result["frames"]) == 1
    assert elapsed < 1.5


def test_poll_youtube_watch_wait_respects_timeout(monkeypatch) -> None:
    """Long-poll returns empty results within the timeout when no data arrives."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch("https://youtu.be/abc123")
    session_id = start["sessionId"]
    started = time.time()
    result = live_video.poll_youtube_watch(session_id, wait_seconds=0.3)
    elapsed = time.time() - started
    assert result["frames"] == []
    assert 0.25 <= elapsed <= 1.0


def test_poll_youtube_watch_can_omit_frame_bytes(monkeypatch) -> None:
    """include_frame_bytes=False returns metadata but no base64 payload."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch("https://youtu.be/abc123")
    session_id = start["sessionId"]
    live_video._require_session(session_id).append_frame(b"\xff\xd8jpeg\xff\xd9", 1, 1)
    result = live_video.poll_youtube_watch(session_id, include_frame_bytes=False)
    assert result["frames"][0].get("jpegBase64") is None
    assert result["frames"][0]["byteLength"] == 8


def test_stop_youtube_watch_removes_session(monkeypatch) -> None:
    """Stopping a session removes it from the registry and reports stopped=true."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch("https://youtu.be/abc123")
    session_id = start["sessionId"]
    listed_before = live_video.list_youtube_watches()
    assert listed_before["count"] == 1
    stopped = live_video.stop_youtube_watch(session_id)
    assert stopped["stopped"] is True
    listed_after = live_video.list_youtube_watches()
    assert listed_after["count"] == 0


def test_unknown_session_raises() -> None:
    """Polling, stopping, and status calls all reject unknown session ids."""
    with pytest.raises(live_video.LiveVideoError):
        live_video.poll_youtube_watch("does-not-exist")
    with pytest.raises(live_video.LiveVideoError):
        live_video.stop_youtube_watch("does-not-exist")
    with pytest.raises(live_video.LiveVideoError):
        live_video.youtube_watch_status("does-not-exist")


def test_session_ring_caps_at_configured_size(monkeypatch) -> None:
    """The frame ring drops old frames when the configured ring size is exceeded."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch("https://youtu.be/abc123", ring_size=4)
    session = live_video._require_session(start["sessionId"])
    for i in range(10):
        session.append_frame(b"\xff\xd8x\xff\xd9", i, i)
    assert len(session.frames) == 4
    # next_frame_sequence keeps counting even after the oldest entries fall off.
    assert session.next_frame_sequence == 11


# JPEG dimension parser ------------------------------------------------------


def test_read_jpeg_dimensions_parses_sof0_marker() -> None:
    """The minimal SOF0 marker test image reports (width, height) correctly."""
    # SOI + SOF0 header with width=320, height=240
    jpeg = (
        b"\xff\xd8"  # SOI
        b"\xff\xc0"  # SOF0
        b"\x00\x11"  # segment length (17 bytes)
        b"\x08"  # precision
        b"\x00\xf0"  # height = 240
        b"\x01\x40"  # width = 320
        b"\x03"  # components
        + b"\x00" * 9
        + b"\xff\xd9"  # EOI
    )
    width, height = live_video._read_jpeg_dimensions(jpeg)
    assert (width, height) == (320, 240)


# Snapshot ------------------------------------------------------------------


def test_youtube_frame_snapshot_runs_ffmpeg_and_returns_jpeg(monkeypatch) -> None:
    """The one-shot frame snapshot path returns the JPEG bytes ffmpeg produced."""
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)
    sample_jpeg = b"\xff\xd8snapshot-bytes\xff\xd9"

    class _Completed:
        """Minimal completed-process stand-in mirroring subprocess.run's return shape."""
        returncode = 0
        stdout = sample_jpeg
        stderr = b""

    monkeypatch.setattr(live_video.subprocess, "run", lambda *a, **kw: _Completed())
    result = live_video.youtube_frame_snapshot("https://youtu.be/abc123")
    assert result["jpegBytes"] == sample_jpeg
    assert result["video"]["title"] == "Test Video"
    assert result["isLive"] is True


def test_youtube_frame_snapshot_rejects_at_seconds_for_live(monkeypatch) -> None:
    """Seeking with at_seconds is rejected for live streams to avoid confusing ffmpeg."""
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)
    with pytest.raises(live_video.LiveVideoError, match="at_seconds"):
        live_video.youtube_frame_snapshot("https://youtu.be/abc123", at_seconds=10.0)


def test_youtube_frame_snapshot_passes_timeout_to_ffmpeg(monkeypatch) -> None:
    """Caller-supplied timeout_seconds reaches subprocess.run so long videos can extend it."""
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)
    sample_jpeg = b"\xff\xd8x\xff\xd9"

    class _Completed:
        returncode = 0
        stdout = sample_jpeg
        stderr = b""

    captured: dict[str, Any] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Completed()

    monkeypatch.setattr(live_video.subprocess, "run", fake_run)
    result = live_video.youtube_frame_snapshot("https://youtu.be/abc123", timeout_seconds=120.0)
    assert captured["timeout"] == 120.0
    assert result["timeoutSeconds"] == 120.0


def test_youtube_frame_snapshot_clamps_timeout(monkeypatch) -> None:
    """timeout_seconds is clamped to the safety ceiling so a runaway request can't hang."""
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)

    class _Completed:
        returncode = 0
        stdout = b"\xff\xd8x\xff\xd9"
        stderr = b""

    captured: dict[str, Any] = {}

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Completed()

    monkeypatch.setattr(live_video.subprocess, "run", fake_run)
    live_video.youtube_frame_snapshot("https://youtu.be/abc123", timeout_seconds=9999.0)
    assert captured["timeout"] == live_video.MAX_FFMPEG_SNAPSHOT_TIMEOUT_SECONDS


def test_caption_poller_holds_back_future_vod_cues(monkeypatch) -> None:
    """On a VOD, the poller must NOT emit cues whose startSeconds > session age.

    Without this guard, a multi-hour video's full transcript would flush into the
    60-slot ring on the first poll, evicting the start of the video and leaving
    the agent seeing captions from the END of the transcript.
    """
    import time as _time

    config = live_video._SessionConfig(
        url="https://youtu.be/abc",
        fps=1.0,
        frame_width=320,
        include_audio=False,
        include_captions=True,
        caption_languages=("en",),
        audio_chunk_seconds=4.0,
        ring_size=200,
        caption_poll_seconds=1.0,
        low_latency=False,
        is_live=False,
        keyframe_diff_threshold=12.0,
        min_step_seconds=1.5,
        quiet_step_seconds=8.0,
    )
    fixed_now = 1_000_000.0
    monkeypatch.setattr(live_video.time, "time", lambda: fixed_now)
    session = live_video._Session(
        session_id="t",
        config=config,
        metadata={},
        started_at=fixed_now - 10.0,  # session is 10s old
        stream_url="http://example/stream",
        audio_stream_url=None,
        caption_track_urls={"en": "http://example/track.vtt"},
    )

    def fake_http(url: str, *, timeout_seconds: float) -> str:
        # Whole-video transcript: cues at 0s, 5s, 10s, 60s, 120s.
        return (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\nA\n\n"
            "00:00:05.000 --> 00:00:06.000\nB\n\n"
            "00:00:10.000 --> 00:00:11.000\nC\n\n"
            "00:01:00.000 --> 00:01:01.000\nD\n\n"
            "00:02:00.000 --> 00:02:01.000\nE\n"
        )

    monkeypatch.setattr(live_video, "_http_get_text", fake_http)

    # One poll, then stop.
    def fake_wait(timeout=None):
        session.stop_event.set()
        return True

    session.stop_event.wait = fake_wait  # type: ignore[method-assign]
    live_video._caption_poller_loop(session)

    emitted_texts = [c.text for c in session.captions]
    assert emitted_texts == ["A", "B", "C"], "future cues (60s, 120s) must be withheld"
    _ = _time  # silence unused-import lint when test rearranges


def test_caption_poller_live_sessions_skip_age_gate(monkeypatch) -> None:
    """Live streams skip the VOD age gate because their tracks only contain past cues."""
    config = live_video._SessionConfig(
        url="https://youtu.be/abc",
        fps=1.0,
        frame_width=320,
        include_audio=False,
        include_captions=True,
        caption_languages=("en",),
        audio_chunk_seconds=4.0,
        ring_size=200,
        caption_poll_seconds=1.0,
        low_latency=True,
        is_live=True,
        keyframe_diff_threshold=12.0,
        min_step_seconds=1.5,
        quiet_step_seconds=8.0,
    )
    fixed_now = 1_000_000.0
    monkeypatch.setattr(live_video.time, "time", lambda: fixed_now)
    session = live_video._Session(
        session_id="t",
        config=config,
        metadata={},
        started_at=fixed_now,  # brand-new session
        stream_url="http://example/stream",
        audio_stream_url=None,
        caption_track_urls={"en": "http://example/track.vtt"},
    )

    def fake_http(url: str, *, timeout_seconds: float) -> str:
        # Live edge timestamps look like wall-clock seconds, way beyond age=0.
        return (
            "WEBVTT\n\n"
            "01:00:00.000 --> 01:00:01.000\nlive cue 1\n\n"
            "01:00:02.000 --> 01:00:03.000\nlive cue 2\n"
        )

    monkeypatch.setattr(live_video, "_http_get_text", fake_http)

    def fake_wait(timeout=None):
        session.stop_event.set()
        return True

    session.stop_event.wait = fake_wait  # type: ignore[method-assign]
    live_video._caption_poller_loop(session)

    assert [c.text for c in session.captions] == ["live cue 1", "live cue 2"]


def test_caption_poller_dedupes_per_cue(monkeypatch) -> None:
    """The poller must emit only NEW cues across repeated fetches of the same VTT track."""
    config = live_video._SessionConfig(
        url="https://youtu.be/abc",
        fps=1.0,
        frame_width=320,
        include_audio=False,
        include_captions=True,
        caption_languages=("en",),
        audio_chunk_seconds=4.0,
        ring_size=200,
        caption_poll_seconds=1.0,
        low_latency=False,
        is_live=False,
        keyframe_diff_threshold=12.0,
        min_step_seconds=1.5,
        quiet_step_seconds=8.0,
    )
    session = live_video._Session(
        session_id="t",
        config=config,
        metadata={},
        started_at=0.0,
        stream_url="http://example/stream",
        audio_stream_url=None,
        caption_track_urls={"en": "http://example/track.vtt"},
    )

    fetch_count = {"n": 0}

    def fake_http(url: str, *, timeout_seconds: float) -> str:
        fetch_count["n"] += 1
        if fetch_count["n"] == 1:
            return "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA\n"
        return (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.000\nA\n\n"
            "00:00:01.500 --> 00:00:02.500\nB\n"
        )

    monkeypatch.setattr(live_video, "_http_get_text", fake_http)

    # Stop the loop after both fetches complete.
    original_wait = session.stop_event.wait
    waits = {"n": 0}

    def fake_wait(timeout=None):
        waits["n"] += 1
        if waits["n"] >= 2:
            session.stop_event.set()
            return True
        return False

    session.stop_event.wait = fake_wait  # type: ignore[method-assign]
    try:
        live_video._caption_poller_loop(session)
    finally:
        session.stop_event.wait = original_wait  # type: ignore[method-assign]

    captions = list(session.captions)
    assert [c.text for c in captions] == ["A", "B"]
    assert captions[0].start_seconds == 0.0 and captions[0].end_seconds == 1.0
    assert captions[1].start_seconds == 1.5 and captions[1].end_seconds == 2.5


# Captions ------------------------------------------------------------------


def test_fetch_youtube_captions_returns_per_cue_records(monkeypatch) -> None:
    """The one-shot caption tool parses fetched VTT bodies into per-cue records with timing."""
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)

    def fake_http(url: str, *, timeout_seconds: float) -> str:
        """Return a tiny VTT body with two cues."""
        return (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:01.500\nHi there\n\n"
            "00:00:02.000 --> 00:00:03.250\nSecond line\n"
        )

    monkeypatch.setattr(live_video, "_http_get_text", fake_http)
    result = live_video.fetch_youtube_captions("https://youtu.be/abc123", languages=("en",))
    en = result["captions"]["en"]
    assert en["cueCount"] == 2
    assert en["truncated"] is False
    assert en["cues"][0] == {"startSeconds": 0.0, "endSeconds": 1.5, "text": "Hi there"}
    assert en["cues"][1] == {"startSeconds": 2.0, "endSeconds": 3.25, "text": "Second line"}
    assert result["availableLanguages"] == ["en"]


def test_fetch_youtube_captions_respects_max_cues(monkeypatch) -> None:
    """max_cues caps cues per language so long videos don't blow the context budget."""
    monkeypatch.setattr(live_video, "_run_ytdlp_metadata", lambda url: SAMPLE_METADATA)
    body_lines = ["WEBVTT", ""]
    for i in range(5):
        body_lines += [f"00:00:0{i}.000 --> 00:00:0{i}.500", f"line {i}", ""]
    monkeypatch.setattr(live_video, "_http_get_text", lambda url, *, timeout_seconds: "\n".join(body_lines))
    result = live_video.fetch_youtube_captions(
        "https://youtu.be/abc123", languages=("en",), max_cues=2
    )
    en = result["captions"]["en"]
    assert en["cueCount"] == 2
    assert en["truncated"] is True
    assert [c["text"] for c in en["cues"]] == ["line 0", "line 1"]


def test_parse_caption_cues_skips_vtt_header_metadata() -> None:
    """Header lines like 'Kind: captions' and 'Language: en' must not become cues."""
    body = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "X-TIMESTAMP-MAP: LOCAL:00:00:00.000,MPEGTS:0\n\n"
        "00:00:01.000 --> 00:00:02.000\nreal cue\n"
    )
    cues = live_video._parse_caption_cues(body)
    assert [c["text"] for c in cues] == ["real cue"]


def test_caption_poller_drops_untimed_cues_on_vod(monkeypatch) -> None:
    """A VOD session must skip cues with null startSeconds (parser leaks, broken rows)."""
    config = live_video._SessionConfig(
        url="https://youtu.be/abc",
        fps=1.0,
        frame_width=320,
        include_audio=False,
        include_captions=True,
        caption_languages=("en",),
        audio_chunk_seconds=4.0,
        ring_size=200,
        caption_poll_seconds=1.0,
        low_latency=False,
        is_live=False,
        keyframe_diff_threshold=12.0,
        min_step_seconds=1.5,
        quiet_step_seconds=8.0,
    )
    fixed_now = 1_000_000.0
    monkeypatch.setattr(live_video.time, "time", lambda: fixed_now)
    session = live_video._Session(
        session_id="t",
        config=config,
        metadata={},
        started_at=fixed_now - 60.0,
        stream_url="http://example/stream",
        audio_stream_url=None,
        caption_track_urls={"en": "http://example/track.vtt"},
    )

    def fake_parse(_body: str):
        return [
            {"startSeconds": None, "endSeconds": None, "text": "junk header leak"},
            {"startSeconds": 1.0, "endSeconds": 2.0, "text": "real cue"},
        ]

    monkeypatch.setattr(live_video, "_parse_caption_cues", fake_parse)
    monkeypatch.setattr(live_video, "_http_get_text", lambda *a, **kw: "ignored")

    def fake_wait(timeout=None):
        session.stop_event.set()
        return True

    session.stop_event.wait = fake_wait  # type: ignore[method-assign]
    live_video._caption_poller_loop(session)
    assert [c.text for c in session.captions] == ["real cue"]


def test_parse_caption_cues_handles_vtt_with_timing() -> None:
    """_parse_caption_cues returns startSeconds/endSeconds parsed from cue timing rows."""
    body = (
        "WEBVTT\n\n"
        "00:00:10.500 --> 00:00:12.750 position:50%\nFirst cue\n\n"
        "00:01:00.000 --> 00:01:02.000\nSecond cue\nspans two lines\n"
    )
    cues = live_video._parse_caption_cues(body)
    assert len(cues) == 2
    assert cues[0]["startSeconds"] == 10.5
    assert cues[0]["endSeconds"] == 12.75
    assert cues[0]["text"] == "First cue"
    assert cues[1]["startSeconds"] == 60.0
    assert cues[1]["endSeconds"] == 62.0
    assert cues[1]["text"] == "Second cue spans two lines"


def test_parse_caption_cues_handles_json3_with_timing() -> None:
    """_parse_caption_cues converts tStartMs/dDurationMs into startSeconds/endSeconds."""
    body = json.dumps({
        "events": [
            {"tStartMs": 1200, "dDurationMs": 800, "segs": [{"utf8": "hello"}]},
            {"tStartMs": 2500, "dDurationMs": 1500, "segs": [{"utf8": "world"}]},
        ]
    })
    cues = live_video._parse_caption_cues(body)
    assert cues[0] == {"startSeconds": 1.2, "endSeconds": 2.0, "text": "hello"}
    assert cues[1] == {"startSeconds": 2.5, "endSeconds": 4.0, "text": "world"}


# Keyframe + step events ---------------------------------------------------


def _make_jpeg(color: tuple[int, int, int], size: tuple[int, int] = (64, 48)) -> bytes:
    """Build a tiny solid-color JPEG so tests can exercise the keyframe diff path."""
    from io import BytesIO

    from PIL import Image as _PILImage

    image = _PILImage.new("RGB", size, color=color)
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def test_keyframe_diff_threshold_collapses_static_frames(monkeypatch) -> None:
    """Two visually identical frames in a row only produce one step."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        min_step_seconds=0.0,  # never throttle in this test
        keyframe_diff_threshold=5.0,
    )
    session = live_video._require_session(start["sessionId"])
    red = _make_jpeg((220, 30, 30))
    # First frame always anchors as a step (initial keyframe).
    session.append_frame(red, 64, 48)
    # Identical frame should NOT trigger a second step.
    session.append_frame(red, 64, 48)
    assert len(session.steps) == 1
    assert session.steps[0].trigger == "keyframe"
    # A clearly different frame should trigger the next step.
    blue = _make_jpeg((30, 30, 220))
    session.append_frame(blue, 64, 48)
    assert len(session.steps) == 2
    assert session.steps[-1].diff_score > 0


def test_step_min_seconds_throttles_rapid_visual_changes(monkeypatch) -> None:
    """min_step_seconds suppresses step events that arrive too close together."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        min_step_seconds=60.0,  # absurdly large so the second keyframe is throttled
        keyframe_diff_threshold=1.0,
    )
    session = live_video._require_session(start["sessionId"])
    session.append_frame(_make_jpeg((10, 10, 10)), 64, 48)
    session.append_frame(_make_jpeg((240, 240, 240)), 64, 48)
    # First frame always emits a step; second is throttled by min_step_seconds.
    assert len(session.steps) == 1


def test_step_records_carry_transcript_and_caption_since_last_step(monkeypatch) -> None:
    """A step event bundles transcripts and captions that arrived since the previous step."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        min_step_seconds=0.0,
        keyframe_diff_threshold=5.0,
    )
    session = live_video._require_session(start["sessionId"])
    session.append_frame(_make_jpeg((10, 10, 10)), 64, 48)
    # Drop transcripts/captions after the first step.
    session.append_transcript("open the terminal", time.time() - 2, time.time())
    session.append_caption("en", "now run npm install")
    # Second visually-different frame -> second step that should bundle those.
    session.append_frame(_make_jpeg((240, 240, 240)), 64, 48)
    assert len(session.steps) == 2
    second = session.steps[-1]
    assert "open the terminal" in second.transcript_text
    assert "npm install" in second.caption_text


def test_quiet_period_step_fires_after_long_silence(monkeypatch) -> None:
    """maybe_emit_quiet_step emits a step when transcripts have been quiet long enough."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        min_step_seconds=0.0,
        quiet_step_seconds=0.1,
        keyframe_diff_threshold=5.0,
    )
    session = live_video._require_session(start["sessionId"])
    session.append_frame(_make_jpeg((10, 10, 10)), 64, 48)
    session.append_transcript("starting now", time.time() - 1, time.time())
    # Force the "last step" timestamp into the past so the quiet check fires.
    session.last_step_at = time.time() - 5
    session.maybe_emit_quiet_step()
    quiet_steps = [step for step in session.steps if step.trigger == "quiet_period"]
    assert len(quiet_steps) == 1
    assert "starting now" in quiet_steps[0].transcript_text


def test_follow_youtube_tutorial_returns_only_new_steps(monkeypatch) -> None:
    """follow_youtube_tutorial returns steps with sequence > since_step_sequence."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        min_step_seconds=0.0,
        keyframe_diff_threshold=5.0,
    )
    session_id = start["sessionId"]
    session = live_video._require_session(session_id)
    session.append_frame(_make_jpeg((10, 10, 10)), 64, 48)
    session.append_frame(_make_jpeg((240, 240, 240)), 64, 48)
    first = live_video.follow_youtube_tutorial(session_id, max_steps=10)
    assert len(first["steps"]) == 2
    assert "jpegBase64" in first["steps"][0]["keyframe"]
    last_seq = first["steps"][-1]["sequence"]
    second = live_video.follow_youtube_tutorial(session_id, since_step_sequence=last_seq)
    assert second["steps"] == []


def test_follow_youtube_tutorial_wait_wakes_on_new_step(monkeypatch) -> None:
    """Long-poll on follow_youtube_tutorial wakes immediately when a step is emitted."""
    _stub_session_workers(monkeypatch)
    start = live_video.start_youtube_watch(
        "https://youtu.be/abc123",
        min_step_seconds=0.0,
        keyframe_diff_threshold=5.0,
    )
    session_id = start["sessionId"]
    session = live_video._require_session(session_id)

    def push_step_after_delay() -> None:
        """Push a visually distinct frame after a short delay to trigger a step."""
        time.sleep(0.15)
        session.append_frame(_make_jpeg((200, 50, 50)), 64, 48)

    threading.Thread(target=push_step_after_delay, daemon=True).start()
    started = time.time()
    result = live_video.follow_youtube_tutorial(session_id, wait_seconds=2.0)
    elapsed = time.time() - started
    assert len(result["steps"]) == 1
    assert elapsed < 1.5


# Status --------------------------------------------------------------------


def test_live_video_status_reports_active_session_count(monkeypatch) -> None:
    """Status reports the active session count and surfaces the configured limits."""
    _stub_session_workers(monkeypatch)
    live_video.start_youtube_watch("https://youtu.be/abc123")
    status = live_video.live_video_status()
    assert status["activeSessions"] == 1
    assert status["limits"]["maxFps"] == live_video.MAX_FPS
