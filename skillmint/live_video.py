"""Live video lane: let MCP agents watch YouTube videos and live streams.

Resolves any yt-dlp-supported URL (YouTube live, regular videos, many other sites),
samples frames via ffmpeg, optionally transcribes audio, and exposes auto-caption
tracks. Designed to be consumed both as one-shot snapshots and as long-running
watch sessions that an agent can poll for new frames/transcription/captions.

Dependencies:
- yt-dlp (Python package, required)
- ffmpeg (system binary, required)
- faster-whisper (optional, only needed for audio transcription)
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .visual_actions import analyze_visual_action

DEFAULT_FRAME_RING_SIZE = 60
DEFAULT_FPS = 2.0
MAX_FPS = 30.0
MAX_FRAME_RING_SIZE = 600
DEFAULT_FRAME_WIDTH = 640
MAX_FRAME_WIDTH = 1920
DEFAULT_AUDIO_CHUNK_SECONDS = 4.0
MIN_AUDIO_CHUNK_SECONDS = 1.5
MAX_AUDIO_CHUNK_SECONDS = 30.0
DEFAULT_CAPTION_POLL_SECONDS = 3.0
MIN_CAPTION_POLL_SECONDS = 1.0
MAX_CAPTION_POLL_SECONDS = 30.0
DEFAULT_POLL_WAIT_SECONDS = 0.0
MAX_POLL_WAIT_SECONDS = 30.0
DEFAULT_POLL_MAX_FRAMES = 5
MAX_POLL_FRAMES = 60
YTDLP_METADATA_TIMEOUT_SECONDS = 25.0
CAPTION_FETCH_TIMEOUT_SECONDS = 10.0
SUBPROCESS_TERMINATE_TIMEOUT_SECONDS = 3.0
DEFAULT_FFMPEG_SNAPSHOT_TIMEOUT_SECONDS = 60.0
MAX_FFMPEG_SNAPSHOT_TIMEOUT_SECONDS = 300.0
CAPTION_FETCH_MAX_BYTES = 16 * 1024 * 1024

# Follow-along (step event) tuning. A keyframe is a frame whose mean-absolute
# pixel difference against the previous keyframe (downsampled to 32x32 gray)
# exceeds KEYFRAME_DIFF_THRESHOLD. A step boundary is emitted when a new
# keyframe lands AND at least MIN_STEP_SECONDS have elapsed since the last
# step; we also flush a pending step when the transcript stays quiet for
# QUIET_STEP_SECONDS, so a long monologue without visual change still produces
# a step the agent can react to.
KEYFRAME_THUMB_SIZE = 32
DEFAULT_KEYFRAME_DIFF_THRESHOLD = 12.0  # 0-255 grayscale MAE
DEFAULT_MIN_STEP_SECONDS = 1.5
DEFAULT_QUIET_STEP_SECONDS = 8.0
DEFAULT_FOLLOW_MAX_STEPS = 4
MAX_FOLLOW_STEPS = 12

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class LiveVideoError(RuntimeError):
    """Raised when a live video operation fails in a way the caller should see."""


@dataclass
class _FrameRecord:
    sequence: int
    captured_at: float
    jpeg_bytes: bytes
    width: int
    height: int


@dataclass
class _TranscriptRecord:
    sequence: int
    started_at: float
    ended_at: float
    text: str


@dataclass
class _CaptionRecord:
    sequence: int
    fetched_at: float
    language: str
    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None


@dataclass
class _StepRecord:
    """One semantic step in a tutorial: a keyframe + everything said since the previous step."""
    sequence: int
    started_at: float
    ended_at: float
    trigger: str  # "keyframe" or "quiet_period"
    keyframe_jpeg: bytes
    keyframe_width: int
    keyframe_height: int
    diff_score: float
    transcript_text: str
    caption_text: str
    seconds_since_previous: float
    visual_action: dict[str, Any] | None = None


@dataclass
class _SessionConfig:
    url: str
    fps: float
    frame_width: int
    include_audio: bool
    include_captions: bool
    caption_languages: tuple[str, ...]
    audio_chunk_seconds: float
    ring_size: int
    caption_poll_seconds: float
    low_latency: bool
    is_live: bool
    keyframe_diff_threshold: float
    min_step_seconds: float
    quiet_step_seconds: float


@dataclass
class _Session:
    session_id: str
    config: _SessionConfig
    metadata: dict[str, Any]
    started_at: float
    stream_url: str
    audio_stream_url: str | None
    caption_track_urls: dict[str, str]
    frames: deque[_FrameRecord] = field(default_factory=deque)
    transcripts: deque[_TranscriptRecord] = field(default_factory=deque)
    captions: deque[_CaptionRecord] = field(default_factory=deque)
    steps: deque[_StepRecord] = field(default_factory=deque)
    next_frame_sequence: int = 1
    next_transcript_sequence: int = 1
    next_caption_sequence: int = 1
    next_step_sequence: int = 1
    last_frame_at: float | None = None
    last_transcript_at: float | None = None
    last_caption_at: float | None = None
    last_step_at: float | None = None
    last_keyframe_thumb: bytes | None = None
    last_step_keyframe_jpeg: bytes | None = None
    last_step_consumed_transcript_seq: int = 0
    last_step_consumed_caption_seq: int = 0
    last_frame_error: str | None = None
    last_audio_error: str | None = None
    last_caption_error: str | None = None
    last_keyframe_error: str | None = None
    video_process: subprocess.Popen[bytes] | None = None
    audio_process: subprocess.Popen[bytes] | None = None
    video_thread: threading.Thread | None = None
    audio_thread: threading.Thread | None = None
    caption_thread: threading.Thread | None = None
    step_thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    new_data_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def append_frame(self, jpeg_bytes: bytes, width: int, height: int) -> None:
        """Append a decoded JPEG frame to the ring, run keyframe detection, and wake pollers."""
        diff_score: float | None = None
        is_keyframe = False
        thumb: bytes | None = None
        try:
            thumb = _compute_frame_thumbnail(jpeg_bytes)
        except Exception as exc:  # noqa: BLE001 - report via session status, keep loop alive
            self.last_keyframe_error = f"thumbnail decode failed: {exc!r}"
        with self.lock:
            record = _FrameRecord(
                sequence=self.next_frame_sequence,
                captured_at=time.time(),
                jpeg_bytes=jpeg_bytes,
                width=width,
                height=height,
            )
            self.next_frame_sequence += 1
            self.frames.append(record)
            while len(self.frames) > self.config.ring_size:
                self.frames.popleft()
            self.last_frame_at = record.captured_at
            if thumb is not None:
                if self.last_keyframe_thumb is None:
                    # The very first frame always anchors as a keyframe so the
                    # agent receives a starting reference point.
                    diff_score = float("inf")
                    is_keyframe = True
                else:
                    diff_score = _thumbnail_mean_abs_diff(self.last_keyframe_thumb, thumb)
                    if diff_score >= self.config.keyframe_diff_threshold:
                        is_keyframe = True
                if is_keyframe:
                    self.last_keyframe_thumb = thumb
        self.new_data_event.set()
        if is_keyframe and thumb is not None:
            self._maybe_emit_step(
                record=record,
                diff_score=diff_score if diff_score is not None else 0.0,
                trigger="keyframe",
            )

    def _maybe_emit_step(
        self,
        *,
        record: _FrameRecord,
        diff_score: float,
        trigger: str,
    ) -> None:
        """Emit a step boundary when enough time has elapsed since the last step."""
        with self.lock:
            now = record.captured_at
            previous_at = self.last_step_at or self.started_at
            elapsed = now - previous_at
            if (
                trigger == "keyframe"
                and self.last_step_at is not None
                and elapsed < self.config.min_step_seconds
            ):
                return
            transcript_text = " ".join(
                transcript.text
                for transcript in self.transcripts
                if transcript.sequence > self.last_step_consumed_transcript_seq
            ).strip()
            caption_text = "\n".join(
                caption.text
                for caption in self.captions
                if caption.sequence > self.last_step_consumed_caption_seq
            ).strip()
            visual_action = analyze_visual_action(
                self.last_step_keyframe_jpeg,
                record.jpeg_bytes,
                diff_score=diff_score if diff_score != float("inf") else None,
                ocr_enabled=False,
            )
            step = _StepRecord(
                sequence=self.next_step_sequence,
                started_at=previous_at,
                ended_at=now,
                trigger=trigger,
                keyframe_jpeg=record.jpeg_bytes,
                keyframe_width=record.width,
                keyframe_height=record.height,
                diff_score=diff_score,
                transcript_text=transcript_text,
                caption_text=caption_text,
                seconds_since_previous=elapsed,
                visual_action=visual_action,
            )
            self.next_step_sequence += 1
            self.steps.append(step)
            while len(self.steps) > self.config.ring_size:
                self.steps.popleft()
            self.last_step_at = now
            self.last_step_keyframe_jpeg = record.jpeg_bytes
            if self.transcripts:
                self.last_step_consumed_transcript_seq = self.transcripts[-1].sequence
            if self.captions:
                self.last_step_consumed_caption_seq = self.captions[-1].sequence
        self.new_data_event.set()

    def maybe_emit_quiet_step(self) -> None:
        """If transcripts have been quiet long enough, flush a quiet-period step."""
        with self.lock:
            if not self.frames:
                return
            now = time.time()
            previous_at = self.last_step_at or self.started_at
            elapsed = now - previous_at
            quiet_threshold = self.config.quiet_step_seconds
            if quiet_threshold <= 0 or elapsed < quiet_threshold:
                return
            transcript_text = " ".join(
                transcript.text
                for transcript in self.transcripts
                if transcript.sequence > self.last_step_consumed_transcript_seq
            ).strip()
            caption_text = "\n".join(
                caption.text
                for caption in self.captions
                if caption.sequence > self.last_step_consumed_caption_seq
            ).strip()
            if not transcript_text and not caption_text:
                # Nothing new happened audibly either; don't spam empty steps.
                return
            latest = self.frames[-1]
            visual_action = analyze_visual_action(
                self.last_step_keyframe_jpeg,
                latest.jpeg_bytes,
                diff_score=0.0,
                ocr_enabled=False,
            )
            step = _StepRecord(
                sequence=self.next_step_sequence,
                started_at=previous_at,
                ended_at=now,
                trigger="quiet_period",
                keyframe_jpeg=latest.jpeg_bytes,
                keyframe_width=latest.width,
                keyframe_height=latest.height,
                diff_score=0.0,
                transcript_text=transcript_text,
                caption_text=caption_text,
                seconds_since_previous=elapsed,
                visual_action=visual_action,
            )
            self.next_step_sequence += 1
            self.steps.append(step)
            while len(self.steps) > self.config.ring_size:
                self.steps.popleft()
            self.last_step_at = now
            self.last_step_keyframe_jpeg = latest.jpeg_bytes
            if self.transcripts:
                self.last_step_consumed_transcript_seq = self.transcripts[-1].sequence
            if self.captions:
                self.last_step_consumed_caption_seq = self.captions[-1].sequence
        self.new_data_event.set()

    def append_transcript(self, text: str, started_at: float, ended_at: float) -> None:
        """Append a transcribed audio chunk to the session."""
        text = text.strip()
        if not text:
            return
        with self.lock:
            record = _TranscriptRecord(
                sequence=self.next_transcript_sequence,
                started_at=started_at,
                ended_at=ended_at,
                text=text,
            )
            self.next_transcript_sequence += 1
            self.transcripts.append(record)
            while len(self.transcripts) > self.config.ring_size:
                self.transcripts.popleft()
            self.last_transcript_at = ended_at
        self.new_data_event.set()

    def append_caption(
        self,
        language: str,
        text: str,
        *,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
    ) -> None:
        """Append a single freshly fetched caption cue."""
        text = text.strip()
        if not text:
            return
        with self.lock:
            record = _CaptionRecord(
                sequence=self.next_caption_sequence,
                fetched_at=time.time(),
                language=language,
                text=text,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
            self.next_caption_sequence += 1
            self.captions.append(record)
            while len(self.captions) > self.config.ring_size:
                self.captions.popleft()
            self.last_caption_at = record.fetched_at
        self.new_data_event.set()


_sessions_lock = threading.Lock()
_sessions: dict[str, _Session] = {}


# yt-dlp wiring ---------------------------------------------------------------


def _require_executable(name: str) -> str:
    """Return the resolved path of a required executable or raise LiveVideoError."""
    path = shutil.which(name)
    if not path:
        raise LiveVideoError(
            f"{name} is required for the live video lane but was not found on PATH."
        )
    return path


def _ytdlp_library_info() -> tuple[bool, str | None]:
    """Return (installed, version) for the yt-dlp Python package."""
    try:
        import yt_dlp
    except ImportError:
        return False, None
    version = getattr(getattr(yt_dlp, "version", None), "__version__", None)
    return True, version


def _run_ytdlp_metadata(url: str) -> dict[str, Any]:
    """Resolve URL metadata via the yt-dlp Python library.

    Calling the library directly avoids the Windows trap where pip installs
    ``yt-dlp.exe`` into a user Scripts dir that is not on PATH, causing the
    previous subprocess-based path to fail with "not found on PATH" even when
    the package is installed.
    """
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise LiveVideoError(
            "yt-dlp Python package is required for the live video lane but is not installed."
        ) from exc
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": YTDLP_METADATA_TIMEOUT_SECONDS,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)
    except DownloadError as exc:
        raise LiveVideoError(f"yt-dlp metadata fetch failed for {url}: {exc}") from exc
    except Exception as exc:
        raise LiveVideoError(f"yt-dlp metadata fetch failed for {url}: {exc}") from exc
    if info is None:
        raise LiveVideoError(f"yt-dlp returned no metadata for {url}")
    return info


def _select_video_format(
    metadata: dict[str, Any],
    *,
    low_latency: bool = False,
) -> dict[str, Any] | None:
    """Pick the best ffmpeg-readable video format from the yt-dlp metadata.

    With low_latency=True, prefer the smallest resolution (smaller HLS segments
    decode and surface to the agent faster) while still requiring a working
    streaming protocol.
    """
    formats = metadata.get("formats") or []
    candidates: list[dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        if not fmt.get("url"):
            continue
        candidates.append(fmt)
    if not candidates:
        if metadata.get("url"):
            return {"url": metadata["url"], "format_id": metadata.get("format_id")}
        return None

    if low_latency:
        def low_latency_score(fmt: dict[str, Any]) -> tuple[int, int, int]:
            """Prefer streaming protocols and smaller resolutions for low latency."""
            protocol = str(fmt.get("protocol") or "").lower()
            protocol_pref = 2 if "m3u8" in protocol else (1 if "http" in protocol else 0)
            # Lower resolution = smaller segments = lower decode/buffer cost.
            height = int(fmt.get("height") or 9999)
            bitrate = int(fmt.get("tbr") or 9999)
            return (protocol_pref, -height, -bitrate)

        candidates.sort(key=low_latency_score, reverse=True)
        return candidates[0]

    def score(fmt: dict[str, Any]) -> tuple[int, int, int]:
        """Prefer streaming protocols at the highest resolution and bitrate."""
        protocol = str(fmt.get("protocol") or "").lower()
        protocol_pref = 2 if "m3u8" in protocol else (1 if "http" in protocol else 0)
        height = int(fmt.get("height") or 0)
        bitrate = int(fmt.get("tbr") or 0)
        return (protocol_pref, height, bitrate)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _select_audio_format(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Pick the best audio-only format if one is available, else None."""
    formats = metadata.get("formats") or []
    audio_only: list[dict[str, Any]] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        if fmt.get("vcodec") not in (None, "none"):
            continue
        if not fmt.get("url"):
            continue
        audio_only.append(fmt)
    if not audio_only:
        return None
    audio_only.sort(key=lambda fmt: int(fmt.get("abr") or 0), reverse=True)
    return audio_only[0]


def _select_caption_tracks(
    metadata: dict[str, Any],
    languages: tuple[str, ...],
) -> dict[str, str]:
    """Map requested language codes to caption track URLs (auto or manual)."""
    chosen: dict[str, str] = {}
    for source_key in ("subtitles", "automatic_captions"):
        source = metadata.get(source_key) or {}
        if not isinstance(source, dict):
            continue
        for lang in languages:
            if lang in chosen:
                continue
            tracks = source.get(lang)
            if not isinstance(tracks, list):
                continue
            preferred_url: str | None = None
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                ext = str(track.get("ext") or "").lower()
                url = track.get("url")
                if not url:
                    continue
                if ext in ("vtt", "srv3", "json3"):
                    preferred_url = url
                    if ext == "vtt":
                        break
                elif preferred_url is None:
                    preferred_url = url
            if preferred_url:
                chosen[lang] = preferred_url
    return chosen


def _summarize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Reduce the verbose yt-dlp metadata blob to a compact agent-friendly summary."""
    available_caption_languages = sorted(
        set((metadata.get("subtitles") or {}).keys())
        | set((metadata.get("automatic_captions") or {}).keys())
    )
    return {
        "id": metadata.get("id"),
        "title": metadata.get("title"),
        "uploader": metadata.get("uploader"),
        "channel": metadata.get("channel"),
        "isLive": bool(metadata.get("is_live")),
        "wasLive": bool(metadata.get("was_live")),
        "liveStatus": metadata.get("live_status"),
        "durationSeconds": metadata.get("duration"),
        "viewCount": metadata.get("view_count"),
        "concurrentViewCount": metadata.get("concurrent_view_count"),
        "uploadDate": metadata.get("upload_date"),
        "webpageUrl": metadata.get("webpage_url") or metadata.get("original_url"),
        "thumbnail": metadata.get("thumbnail"),
        "availableCaptionLanguages": available_caption_languages,
        "hasAutomaticCaptions": bool(metadata.get("automatic_captions")),
    }


# Caption fetch / parse -------------------------------------------------------


def _http_get_text(url: str, *, timeout_seconds: float) -> str:
    """Fetch a URL and return its body as text, with a safety cap.

    The cap is generous (16 MiB) because a multi-hour video's VTT track can run
    well past the old 2 MiB limit and used to be silently truncated.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Periphery-LiveVideo/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read(CAPTION_FETCH_MAX_BYTES)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LiveVideoError(f"caption fetch failed: {exc}") from exc
    return raw.decode("utf-8", errors="replace")


def _parse_vtt_timestamp(token: str) -> float | None:
    """Parse a WebVTT/SRT timestamp (HH:MM:SS.mmm or MM:SS.mmm, ',' or '.') to seconds."""
    token = token.strip().replace(",", ".")
    if not token:
        return None
    parts = token.split(":")
    try:
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        if len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        if len(parts) == 1:
            return float(parts[0])
    except ValueError:
        return None
    return None


def _parse_caption_cues(body: str) -> list[dict[str, Any]]:
    """Parse a VTT, SRT, or YouTube json3 caption track into individual cues.

    Each cue is ``{"startSeconds": float|None, "endSeconds": float|None, "text": str}``.
    Returning per-cue records is what lets the watch session emit one record per
    cue (with timing) instead of dumping the entire transcript as one giant blob —
    which used to make a multi-hour course unusable.
    """
    body = body.strip()
    if not body:
        return []
    cues: list[dict[str, Any]] = []
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return [{"startSeconds": None, "endSeconds": None, "text": body}]
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            return []
        for event in events:
            if not isinstance(event, dict):
                continue
            segments = event.get("segs") or []
            line_parts = [
                str(seg.get("utf8", ""))
                for seg in segments
                if isinstance(seg, dict)
            ]
            text = "".join(line_parts).strip()
            if not text:
                continue
            start_ms = event.get("tStartMs")
            dur_ms = event.get("dDurationMs")
            start_s = float(start_ms) / 1000.0 if isinstance(start_ms, (int, float)) else None
            end_s = (
                start_s + float(dur_ms) / 1000.0
                if start_s is not None and isinstance(dur_ms, (int, float))
                else None
            )
            cues.append({"startSeconds": start_s, "endSeconds": end_s, "text": text})
        return cues
    # WEBVTT / SRT: split on blank lines, each block is one cue.
    current_start: float | None = None
    current_end: float | None = None
    current_lines: list[str] = []

    def flush() -> None:
        if not current_lines:
            return
        text = " ".join(current_lines).strip()
        if text:
            cues.append({
                "startSeconds": current_start,
                "endSeconds": current_end,
                "text": text,
            })

    in_header = True
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            current_start = None
            current_end = None
            current_lines = []
            in_header = False
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE") or line.startswith("STYLE"):
            continue
        # VTT/YouTube header metadata lives between WEBVTT and the first blank
        # line: "Kind: captions", "Language: en", "X-TIMESTAMP-MAP: ...", etc.
        # Never treat these as cue content.
        if in_header and ":" in line and "-->" not in line:
            continue
        if line.isdigit():
            # Cue index line in SRT — ignore.
            continue
        if "-->" in line:
            left, _, right = line.partition("-->")
            right_token = right.strip().split(" ", 1)[0]  # drop WebVTT cue settings
            current_start = _parse_vtt_timestamp(left)
            current_end = _parse_vtt_timestamp(right_token)
            in_header = False
            continue
        current_lines.append(line)
    flush()
    return cues


def _parse_caption_text(body: str) -> str:
    """Back-compat helper: flatten parsed cues to newline-joined text."""
    return "\n".join(cue["text"] for cue in _parse_caption_cues(body) if cue.get("text"))


def fetch_youtube_captions(
    url: str,
    *,
    languages: tuple[str, ...] = ("en",),
    max_cues: int | None = None,
) -> dict[str, Any]:
    """One-shot caption fetch for a YouTube video without starting a session.

    Returns per-language cue lists with per-cue start/end timestamps. ``max_cues``
    caps the number of cues returned per language (None = no cap). For multi-hour
    videos, callers should always pass ``max_cues`` to avoid blowing their context
    budget with the entire transcript.
    """
    metadata = _run_ytdlp_metadata(url)
    tracks = _select_caption_tracks(metadata, languages)
    captions: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for lang, track_url in tracks.items():
        try:
            body = _http_get_text(track_url, timeout_seconds=CAPTION_FETCH_TIMEOUT_SECONDS)
            cues = _parse_caption_cues(body)
            truncated = False
            if max_cues is not None and len(cues) > int(max_cues):
                cues = cues[: int(max_cues)]
                truncated = True
            captions[lang] = {
                "language": lang,
                "trackUrl": track_url,
                "cues": cues,
                "cueCount": len(cues),
                "truncated": truncated,
            }
        except LiveVideoError as exc:
            errors[lang] = str(exc)
    return {
        "url": url,
        "video": _summarize_metadata(metadata),
        "requestedLanguages": list(languages),
        "availableLanguages": list(tracks.keys()),
        "captions": captions,
        "errors": errors,
    }


# One-shot frame snapshot -----------------------------------------------------


def youtube_frame_snapshot(
    url: str,
    *,
    at_seconds: float | None = None,
    frame_width: int = DEFAULT_FRAME_WIDTH,
    quality: int = 5,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Pull a single JPEG frame from a YouTube URL via ffmpeg and return its bytes.

    ``timeout_seconds`` covers only the ffmpeg subprocess; the preceding yt-dlp
    metadata fetch has its own timeout. Long videos with cold HLS caches can take
    longer than the snapshot default, so a multi-hour course should bump this.
    """
    metadata = _run_ytdlp_metadata(url)
    is_live = bool(metadata.get("is_live"))
    if at_seconds is not None and is_live:
        raise LiveVideoError(
            "at_seconds is not supported for live streams; omit it to grab the latest frame."
        )
    video_format = _select_video_format(metadata)
    if not video_format:
        raise LiveVideoError(f"yt-dlp returned no playable video format for {url}")

    ffmpeg = _require_executable("ffmpeg")
    width = max(64, min(int(frame_width), MAX_FRAME_WIDTH))
    quality = max(2, min(int(quality), 31))
    if timeout_seconds is None:
        effective_timeout = DEFAULT_FFMPEG_SNAPSHOT_TIMEOUT_SECONDS
    else:
        effective_timeout = max(
            1.0,
            min(float(timeout_seconds), MAX_FFMPEG_SNAPSHOT_TIMEOUT_SECONDS),
        )
    args: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if at_seconds is not None:
        args += ["-ss", f"{max(0.0, float(at_seconds)):.3f}"]
    args += [
        "-i",
        video_format["url"],
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        str(quality),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - explicit executable
            args,
            check=False,
            capture_output=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LiveVideoError(
            f"ffmpeg frame snapshot timed out after {effective_timeout:.1f}s for {url}"
        ) from exc
    if completed.returncode != 0 or not completed.stdout:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise LiveVideoError(f"ffmpeg frame snapshot failed: {stderr or 'no stderr'}")
    return {
        "url": url,
        "video": _summarize_metadata(metadata),
        "atSeconds": at_seconds,
        "frameWidth": width,
        "quality": quality,
        "timeoutSeconds": effective_timeout,
        "jpegBytes": completed.stdout,
        "format": "jpeg",
        "isLive": is_live,
    }


# Frame extractor thread ------------------------------------------------------


def _frame_extractor_loop(session: _Session) -> None:
    """Read JPEG frames out of ffmpeg's stdout pipe and append them to the session."""
    process = session.video_process
    if process is None or process.stdout is None:
        return
    buffer = bytearray()
    chunk_size = 64 * 1024
    try:
        while not session.stop_event.is_set():
            chunk = process.stdout.read(chunk_size)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(JPEG_SOI)
                if start < 0:
                    if len(buffer) > 1024 * 1024:
                        del buffer[:-2]
                    break
                end = buffer.find(JPEG_EOI, start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break
                end += 2
                jpeg_bytes = bytes(buffer[start:end])
                del buffer[:end]
                width, height = _read_jpeg_dimensions(jpeg_bytes)
                session.append_frame(jpeg_bytes, width, height)
    except Exception as exc:  # noqa: BLE001 - surfaced via session status
        session.last_frame_error = f"frame extractor crashed: {exc!r}"
    finally:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def _compute_frame_thumbnail(jpeg_bytes: bytes) -> bytes:
    """Decode a JPEG and return a tiny grayscale thumbnail used for keyframe diffing."""
    from io import BytesIO

    from PIL import Image as _PILImage

    with _PILImage.open(BytesIO(jpeg_bytes)) as image:
        thumb = image.convert("L").resize(
            (KEYFRAME_THUMB_SIZE, KEYFRAME_THUMB_SIZE),
            _PILImage.Resampling.BILINEAR,
        )
        return thumb.tobytes()


def _thumbnail_mean_abs_diff(left: bytes, right: bytes) -> float:
    """Return the mean absolute pixel difference between two equal-length grayscale buffers."""
    if len(left) != len(right) or not left:
        return float("inf")
    total = 0
    for a, b in zip(left, right, strict=True):
        total += a - b if a >= b else b - a
    return total / len(left)


def _read_jpeg_dimensions(jpeg_bytes: bytes) -> tuple[int, int]:
    """Parse JPEG SOFx markers for image dimensions, returning (0, 0) on failure."""
    i = 2
    n = len(jpeg_bytes)
    while i + 9 < n:
        if jpeg_bytes[i] != 0xFF:
            return (0, 0)
        marker = jpeg_bytes[i + 1]
        i += 2
        if marker in (0xD8, 0xD9):
            continue
        if 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > n:
            return (0, 0)
        segment_length = (jpeg_bytes[i] << 8) | jpeg_bytes[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 7 > n:
                return (0, 0)
            height = (jpeg_bytes[i + 3] << 8) | jpeg_bytes[i + 4]
            width = (jpeg_bytes[i + 5] << 8) | jpeg_bytes[i + 6]
            return (width, height)
        i += segment_length
    return (0, 0)


def _start_video_extractor(session: _Session) -> None:
    """Spawn ffmpeg for the video pipeline and start the decoder thread."""
    ffmpeg = _require_executable("ffmpeg")
    fps = max(0.1, min(float(session.config.fps), MAX_FPS))
    width = max(64, min(int(session.config.frame_width), MAX_FRAME_WIDTH))
    low_latency = session.config.low_latency or session.config.is_live
    # In low-latency mode we tell ffmpeg to skip its input buffering, dump
    # demuxer probes, and emit decoded frames as soon as they're ready. This
    # shaves seconds off the agent-perceived lag on live HLS sources.
    args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if low_latency:
        args += [
            "-fflags",
            "+nobuffer+flush_packets+discardcorrupt",
            "-flags",
            "low_delay",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-rtbufsize",
            "1M",
        ]
    else:
        args += ["-fflags", "+nobuffer"]
    args += [
        "-i",
        session.stream_url,
        "-an",
        "-sn",
        "-vf",
        f"fps={fps},scale={width}:-2",
        "-q:v",
        "6",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    process = subprocess.Popen(  # noqa: S603 - explicit executable
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    session.video_process = process
    thread = threading.Thread(
        target=_frame_extractor_loop,
        args=(session,),
        name=f"skillmint-live-video-frames-{session.session_id[:8]}",
        daemon=True,
    )
    session.video_thread = thread
    thread.start()


# Audio extractor / transcription --------------------------------------------


def _whisper_available() -> tuple[bool, str | None]:
    """Probe for an installed Whisper backend without importing it eagerly."""
    try:
        import faster_whisper  # type: ignore  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - probe should never raise upward
        return (False, f"faster-whisper not importable: {exc!r}")
    return (True, None)


def _audio_extractor_loop(session: _Session) -> None:
    """Stream audio chunks out of ffmpeg, transcribe each, and append the text."""
    available, reason = _whisper_available()
    if not available:
        session.last_audio_error = reason or "transcription backend unavailable"
        return
    from faster_whisper import WhisperModel  # type: ignore

    process = session.audio_process
    if process is None or process.stdout is None:
        return

    model_name = os.environ.get("SKILLMINT_WHISPER_MODEL", "tiny")
    try:
        model = WhisperModel(model_name, compute_type="int8")
    except Exception as exc:  # noqa: BLE001 - surfaced via session status
        session.last_audio_error = f"whisper model load failed: {exc!r}"
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        return

    sample_rate = 16000
    bytes_per_sample = 2
    chunk_seconds = max(
        MIN_AUDIO_CHUNK_SECONDS,
        min(float(session.config.audio_chunk_seconds), MAX_AUDIO_CHUNK_SECONDS),
    )
    chunk_bytes = int(sample_rate * bytes_per_sample * chunk_seconds)
    buffer = bytearray()
    try:
        while not session.stop_event.is_set():
            chunk = process.stdout.read(chunk_bytes)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) < chunk_bytes:
                continue
            started_at = time.time() - chunk_seconds
            ended_at = time.time()
            samples = _pcm16_to_float32(bytes(buffer[:chunk_bytes]))
            del buffer[:chunk_bytes]
            try:
                segments, _info = model.transcribe(samples, language=None, vad_filter=True)
                text = " ".join(segment.text for segment in segments).strip()
            except Exception as exc:  # noqa: BLE001 - keep loop alive
                session.last_audio_error = f"whisper transcribe failed: {exc!r}"
                continue
            session.append_transcript(text, started_at, ended_at)
    except Exception as exc:  # noqa: BLE001 - surfaced via session status
        session.last_audio_error = f"audio extractor crashed: {exc!r}"
    finally:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def _pcm16_to_float32(pcm: bytes) -> Any:
    """Convert signed-16 little-endian PCM bytes to a float32 numpy array."""
    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # noqa: BLE001 - whisper already implies numpy
        raise LiveVideoError(f"numpy required for audio transcription: {exc!r}") from exc
    raw = np.frombuffer(pcm, dtype=np.int16)
    return (raw.astype(np.float32) / 32768.0).copy()


def _start_audio_extractor(session: _Session) -> None:
    """Spawn ffmpeg for the audio pipeline if a usable audio track was resolved."""
    if not session.config.include_audio:
        return
    audio_source = session.audio_stream_url or session.stream_url
    ffmpeg = _require_executable("ffmpeg")
    low_latency = session.config.low_latency or session.config.is_live
    args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if low_latency:
        args += [
            "-fflags",
            "+nobuffer+flush_packets+discardcorrupt",
            "-flags",
            "low_delay",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
        ]
    args += [
        "-i",
        audio_source,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    process = subprocess.Popen(  # noqa: S603 - explicit executable
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    session.audio_process = process
    thread = threading.Thread(
        target=_audio_extractor_loop,
        args=(session,),
        name=f"skillmint-live-video-audio-{session.session_id[:8]}",
        daemon=True,
    )
    session.audio_thread = thread
    thread.start()


# Caption poller --------------------------------------------------------------


def _caption_poller_loop(session: _Session) -> None:
    """Periodically refetch caption tracks and emit one record per new cue.

    YouTube serves the full caption track on every fetch, even for static videos.
    Two-layer guard:
    1. Per-cue dedup: keep ``(startSeconds, endSeconds, text)`` seen keys per
       language so a refetched track doesn't re-emit cues we already surfaced.
    2. VOD playback gating: for non-live sessions, only emit cues whose
       ``startSeconds`` is <= the session age. Otherwise a 4-hour course flushes
       all ~13k cues into the 60-slot ring on the first poll, evicting the
       beginning of the transcript and leaving the agent looking at cues from
       the end of the video instead of "what's playing now". Live streams skip
       this filter because their tracks only contain cues up to the live edge
       already.
    """
    poll_interval = max(
        MIN_CAPTION_POLL_SECONDS,
        min(float(session.config.caption_poll_seconds), MAX_CAPTION_POLL_SECONDS),
    )
    seen_keys_by_lang: dict[str, set[tuple[float | None, float | None, str]]] = {}
    is_vod = not session.config.is_live
    while not session.stop_event.is_set():
        age_seconds = time.time() - session.started_at
        for lang, track_url in session.caption_track_urls.items():
            if session.stop_event.is_set():
                return
            try:
                body = _http_get_text(track_url, timeout_seconds=CAPTION_FETCH_TIMEOUT_SECONDS)
            except LiveVideoError as exc:
                session.last_caption_error = f"caption fetch failed for {lang}: {exc}"
                continue
            cues = _parse_caption_cues(body)
            if not cues:
                continue
            seen = seen_keys_by_lang.setdefault(lang, set())
            for cue in cues:
                text = cue.get("text", "")
                if not text:
                    continue
                start_s = cue.get("startSeconds")
                if is_vod:
                    # Drop cues without timing on VODs (VTT header leaks, broken
                    # karaoke rows) — they can't be placed against playback.
                    if not isinstance(start_s, (int, float)):
                        continue
                    # Hold back cues whose simulated playback hasn't reached them
                    # yet — they get reconsidered on a subsequent poll.
                    if start_s > age_seconds:
                        continue
                key = (start_s, cue.get("endSeconds"), text)
                if key in seen:
                    continue
                seen.add(key)
                session.append_caption(
                    lang,
                    text,
                    start_seconds=start_s,
                    end_seconds=cue.get("endSeconds"),
                )
        if session.stop_event.wait(poll_interval):
            return


def _start_caption_poller(session: _Session) -> None:
    """Start the caption poll thread if we resolved any caption tracks."""
    if not session.config.include_captions or not session.caption_track_urls:
        return
    thread = threading.Thread(
        target=_caption_poller_loop,
        args=(session,),
        name=f"skillmint-live-video-captions-{session.session_id[:8]}",
        daemon=True,
    )
    session.caption_thread = thread
    thread.start()


def _step_watchdog_loop(session: _Session) -> None:
    """Background thread that flushes quiet-period step events for follow-along agents."""
    interval = max(0.5, session.config.min_step_seconds / 2.0)
    while not session.stop_event.is_set():
        try:
            session.maybe_emit_quiet_step()
        except Exception as exc:  # noqa: BLE001 - watchdog must not crash the session
            session.last_keyframe_error = f"step watchdog error: {exc!r}"
        if session.stop_event.wait(interval):
            return


def _start_step_watchdog(session: _Session) -> None:
    """Start the step watchdog so quiet-period steps fire without depending on captions."""
    thread = threading.Thread(
        target=_step_watchdog_loop,
        args=(session,),
        name=f"skillmint-live-video-steps-{session.session_id[:8]}",
        daemon=True,
    )
    thread.start()


# Session lifecycle -----------------------------------------------------------


def start_youtube_watch(
    url: str,
    *,
    fps: float = DEFAULT_FPS,
    frame_width: int = DEFAULT_FRAME_WIDTH,
    include_audio: bool = False,
    include_captions: bool = True,
    caption_languages: tuple[str, ...] = ("en",),
    audio_chunk_seconds: float = DEFAULT_AUDIO_CHUNK_SECONDS,
    ring_size: int = DEFAULT_FRAME_RING_SIZE,
    caption_poll_seconds: float = DEFAULT_CAPTION_POLL_SECONDS,
    low_latency: bool | None = None,
    keyframe_diff_threshold: float = DEFAULT_KEYFRAME_DIFF_THRESHOLD,
    min_step_seconds: float = DEFAULT_MIN_STEP_SECONDS,
    quiet_step_seconds: float = DEFAULT_QUIET_STEP_SECONDS,
) -> dict[str, Any]:
    """Begin a long-running watch session and return its session metadata."""
    metadata = _run_ytdlp_metadata(url)
    is_live = bool(metadata.get("is_live"))
    # Live streams default to low-latency selection and ffmpeg flags. Callers
    # can force low_latency on VODs to trade resolution for faster decode/decode.
    resolved_low_latency = bool(is_live if low_latency is None else low_latency)
    video_format = _select_video_format(metadata, low_latency=resolved_low_latency)
    if not video_format:
        raise LiveVideoError(f"yt-dlp returned no playable video format for {url}")
    audio_format = _select_audio_format(metadata) if include_audio else None
    caption_tracks = (
        _select_caption_tracks(metadata, caption_languages) if include_captions else {}
    )
    config = _SessionConfig(
        url=url,
        fps=max(0.1, min(float(fps), MAX_FPS)),
        frame_width=max(64, min(int(frame_width), MAX_FRAME_WIDTH)),
        include_audio=bool(include_audio),
        include_captions=bool(include_captions),
        caption_languages=tuple(caption_languages),
        audio_chunk_seconds=max(
            MIN_AUDIO_CHUNK_SECONDS,
            min(float(audio_chunk_seconds), MAX_AUDIO_CHUNK_SECONDS),
        ),
        ring_size=max(4, min(int(ring_size), MAX_FRAME_RING_SIZE)),
        caption_poll_seconds=max(
            MIN_CAPTION_POLL_SECONDS,
            min(float(caption_poll_seconds), MAX_CAPTION_POLL_SECONDS),
        ),
        low_latency=resolved_low_latency,
        is_live=is_live,
        keyframe_diff_threshold=max(0.0, float(keyframe_diff_threshold)),
        min_step_seconds=max(0.0, float(min_step_seconds)),
        quiet_step_seconds=max(0.0, float(quiet_step_seconds)),
    )
    summary = _summarize_metadata(metadata)
    session = _Session(
        session_id=uuid.uuid4().hex,
        config=config,
        metadata=summary,
        started_at=time.time(),
        stream_url=video_format["url"],
        audio_stream_url=audio_format["url"] if audio_format else None,
        caption_track_urls=caption_tracks,
    )
    _start_video_extractor(session)
    _start_audio_extractor(session)
    _start_caption_poller(session)
    _start_step_watchdog(session)
    with _sessions_lock:
        _sessions[session.session_id] = session
    return _session_status_payload(session, include_config=True)


def stop_youtube_watch(session_id: str) -> dict[str, Any]:
    """Stop a watch session and tear down its ffmpeg / poll threads."""
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        raise LiveVideoError(f"unknown live video session: {session_id}")
    _shutdown_session(session)
    return {"sessionId": session_id, "stopped": True, "status": _session_status_payload(session)}


def _shutdown_session(session: _Session) -> None:
    """Signal all worker threads to exit and best-effort terminate ffmpeg processes."""
    session.stop_event.set()
    session.new_data_event.set()
    for process in (session.video_process, session.audio_process):
        if process is None:
            continue
        if process.poll() is not None:
            continue
        try:
            process.terminate()
        except OSError:
            continue
        try:
            process.wait(timeout=SUBPROCESS_TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
    session.closed = True


def list_youtube_watches() -> dict[str, Any]:
    """Return a snapshot of every active watch session."""
    with _sessions_lock:
        sessions = list(_sessions.values())
    return {
        "count": len(sessions),
        "sessions": [_session_status_payload(session) for session in sessions],
    }


def youtube_watch_status(session_id: str) -> dict[str, Any]:
    """Return a single session's current status without modifying it."""
    session = _require_session(session_id)
    return _session_status_payload(session, include_config=True)


def _require_session(session_id: str) -> _Session:
    """Return a session by id or raise LiveVideoError if it does not exist."""
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise LiveVideoError(f"unknown live video session: {session_id}")
    return session


def _session_status_payload(
    session: _Session,
    *,
    include_config: bool = False,
) -> dict[str, Any]:
    """Build the status payload reported by start/status/list tools."""
    with session.lock:
        frame_count = len(session.frames)
        transcript_count = len(session.transcripts)
        caption_count = len(session.captions)
        step_count = len(session.steps)
        last_frame_at = session.last_frame_at
        last_transcript_at = session.last_transcript_at
        last_caption_at = session.last_caption_at
        last_step_at = session.last_step_at
    payload: dict[str, Any] = {
        "sessionId": session.session_id,
        "url": session.config.url,
        "startedAt": session.started_at,
        "ageSeconds": max(0.0, time.time() - session.started_at),
        "video": session.metadata,
        "frameCount": frame_count,
        "transcriptCount": transcript_count,
        "captionCount": caption_count,
        "stepCount": step_count,
        "lastFrameAt": last_frame_at,
        "lastTranscriptAt": last_transcript_at,
        "lastCaptionAt": last_caption_at,
        "lastStepAt": last_step_at,
        "nextFrameSequence": session.next_frame_sequence,
        "nextTranscriptSequence": session.next_transcript_sequence,
        "nextCaptionSequence": session.next_caption_sequence,
        "nextStepSequence": session.next_step_sequence,
        "audioEnabled": session.config.include_audio,
        "captionsEnabled": session.config.include_captions,
        "captionLanguagesResolved": sorted(session.caption_track_urls.keys()),
        "frameError": session.last_frame_error,
        "audioError": session.last_audio_error,
        "captionError": session.last_caption_error,
        "keyframeError": session.last_keyframe_error,
        "closed": session.closed,
    }
    if include_config:
        payload["config"] = {
            "fps": session.config.fps,
            "frameWidth": session.config.frame_width,
            "captionLanguagesRequested": list(session.config.caption_languages),
            "audioChunkSeconds": session.config.audio_chunk_seconds,
            "ringSize": session.config.ring_size,
            "captionPollSeconds": session.config.caption_poll_seconds,
            "lowLatency": session.config.low_latency,
            "isLive": session.config.is_live,
            "keyframeDiffThreshold": session.config.keyframe_diff_threshold,
            "minStepSeconds": session.config.min_step_seconds,
            "quietStepSeconds": session.config.quiet_step_seconds,
        }
    return payload


# Polling ---------------------------------------------------------------------


def poll_youtube_watch(
    session_id: str,
    *,
    since_frame_sequence: int | None = None,
    since_transcript_sequence: int | None = None,
    since_caption_sequence: int | None = None,
    max_frames: int = DEFAULT_POLL_MAX_FRAMES,
    wait_seconds: float = DEFAULT_POLL_WAIT_SECONDS,
    include_frame_bytes: bool = True,
) -> dict[str, Any]:
    """Return new frames, transcripts, and captions since the supplied sequences."""
    session = _require_session(session_id)
    max_frames = max(1, min(int(max_frames), MAX_POLL_FRAMES))
    wait_seconds = max(0.0, min(float(wait_seconds), MAX_POLL_WAIT_SECONDS))
    deadline = time.time() + wait_seconds

    while True:
        new_frames, new_transcripts, new_captions = _collect_new(
            session,
            since_frame_sequence,
            since_transcript_sequence,
            since_caption_sequence,
            max_frames,
        )
        has_new = bool(new_frames or new_transcripts or new_captions)
        if has_new or wait_seconds <= 0.0:
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        session.new_data_event.clear()
        session.new_data_event.wait(timeout=min(remaining, 0.5))
        if time.time() >= deadline:
            break

    frames_payload = []
    for frame in new_frames:
        frame_entry: dict[str, Any] = {
            "sequence": frame.sequence,
            "capturedAt": frame.captured_at,
            "width": frame.width,
            "height": frame.height,
            "format": "jpeg",
            "byteLength": len(frame.jpeg_bytes),
        }
        if include_frame_bytes:
            frame_entry["jpegBase64"] = base64.b64encode(frame.jpeg_bytes).decode("ascii")
        frames_payload.append(frame_entry)

    return {
        "sessionId": session.session_id,
        "status": _session_status_payload(session),
        "frames": frames_payload,
        "transcripts": [
            {
                "sequence": transcript.sequence,
                "startedAt": transcript.started_at,
                "endedAt": transcript.ended_at,
                "text": transcript.text,
            }
            for transcript in new_transcripts
        ],
        "captions": [
            {
                "sequence": caption.sequence,
                "fetchedAt": caption.fetched_at,
                "language": caption.language,
                "text": caption.text,
                "startSeconds": caption.start_seconds,
                "endSeconds": caption.end_seconds,
            }
            for caption in new_captions
        ],
        "framesIncludeBytes": bool(include_frame_bytes),
        "waitedSeconds": max(0.0, wait_seconds - max(0.0, deadline - time.time())),
    }


def _collect_new(
    session: _Session,
    since_frame_sequence: int | None,
    since_transcript_sequence: int | None,
    since_caption_sequence: int | None,
    max_frames: int,
) -> tuple[list[_FrameRecord], list[_TranscriptRecord], list[_CaptionRecord]]:
    """Snapshot the session ring buffers under lock and slice them by since-sequence."""
    with session.lock:
        frames = list(session.frames)
        transcripts = list(session.transcripts)
        captions = list(session.captions)

    if since_frame_sequence is not None:
        frames = [frame for frame in frames if frame.sequence > int(since_frame_sequence)]
    if since_transcript_sequence is not None:
        transcripts = [
            transcript
            for transcript in transcripts
            if transcript.sequence > int(since_transcript_sequence)
        ]
    if since_caption_sequence is not None:
        captions = [
            caption
            for caption in captions
            if caption.sequence > int(since_caption_sequence)
        ]
    if len(frames) > max_frames:
        frames = frames[-max_frames:]
    return frames, transcripts, captions


# Follow-along ---------------------------------------------------------------


def follow_youtube_tutorial(
    session_id: str,
    *,
    since_step_sequence: int | None = None,
    max_steps: int = DEFAULT_FOLLOW_MAX_STEPS,
    wait_seconds: float = DEFAULT_POLL_WAIT_SECONDS,
    include_keyframe_bytes: bool = True,
) -> dict[str, Any]:
    """Return the next batch of step events for an agent following a tutorial.

    Each step bundles one keyframe (the frame whose visual diff crossed the
    keyframe threshold, or the latest frame if the step was triggered by a
    quiet period in the transcript) plus all transcript and caption text that
    accumulated between the previous step boundary and this one. The agent
    consumes steps as a logical "next thing happened" stream rather than
    redundant frame-by-frame polling.
    """
    session = _require_session(session_id)
    max_steps = max(1, min(int(max_steps), MAX_FOLLOW_STEPS))
    wait_seconds = max(0.0, min(float(wait_seconds), MAX_POLL_WAIT_SECONDS))
    deadline = time.time() + wait_seconds

    while True:
        new_steps = _collect_new_steps(session, since_step_sequence, max_steps)
        if new_steps or wait_seconds <= 0.0:
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        session.new_data_event.clear()
        session.new_data_event.wait(timeout=min(remaining, 0.5))
        if time.time() >= deadline:
            break

    steps_payload = []
    for step in new_steps:
        entry: dict[str, Any] = {
            "sequence": step.sequence,
            "startedAt": step.started_at,
            "endedAt": step.ended_at,
            "durationSeconds": max(0.0, step.ended_at - step.started_at),
            "trigger": step.trigger,
            "diffScore": step.diff_score if step.diff_score != float("inf") else None,
            "secondsSincePrevious": step.seconds_since_previous,
            "keyframe": {
                "width": step.keyframe_width,
                "height": step.keyframe_height,
                "format": "jpeg",
                "byteLength": len(step.keyframe_jpeg),
            },
            "transcript": step.transcript_text,
            "captions": step.caption_text,
            "visualAction": step.visual_action,
        }
        if include_keyframe_bytes:
            entry["keyframe"]["jpegBase64"] = base64.b64encode(step.keyframe_jpeg).decode("ascii")
        steps_payload.append(entry)

    return {
        "sessionId": session.session_id,
        "status": _session_status_payload(session),
        "steps": steps_payload,
        "stepsIncludeBytes": bool(include_keyframe_bytes),
        "waitedSeconds": max(0.0, wait_seconds - max(0.0, deadline - time.time())),
    }


def _collect_new_steps(
    session: _Session,
    since_step_sequence: int | None,
    max_steps: int,
) -> list[_StepRecord]:
    """Snapshot the session step ring under lock and slice by since-sequence."""
    with session.lock:
        steps = list(session.steps)
    if since_step_sequence is not None:
        steps = [step for step in steps if step.sequence > int(since_step_sequence)]
    if len(steps) > max_steps:
        steps = steps[-max_steps:]
    return steps


def get_session_step_snapshot(session_id: str) -> dict[str, Any]:
    """Return a deep snapshot of a session's accumulated steps for persistence.

    Returned dict keys: sessionId, url, video (compact metadata), startedAt,
    config, and steps (list of dicts with raw bytes for keyframes). Designed
    so tutorial_playbooks can write the snapshot to disk without reaching
    into live_video's private state.
    """
    session = _require_session(session_id)
    with session.lock:
        steps = list(session.steps)
        transcripts = list(session.transcripts)
        captions = list(session.captions)
    return {
        "sessionId": session.session_id,
        "url": session.config.url,
        "video": dict(session.metadata),
        "startedAt": session.started_at,
        "config": {
            "fps": session.config.fps,
            "frameWidth": session.config.frame_width,
            "isLive": session.config.is_live,
            "keyframeDiffThreshold": session.config.keyframe_diff_threshold,
            "minStepSeconds": session.config.min_step_seconds,
            "quietStepSeconds": session.config.quiet_step_seconds,
        },
        "steps": [
            {
                "sequence": step.sequence,
                "startedAt": step.started_at,
                "endedAt": step.ended_at,
                "trigger": step.trigger,
                "diffScore": (
                    step.diff_score if step.diff_score != float("inf") else None
                ),
                "secondsSincePrevious": step.seconds_since_previous,
                "keyframeJpeg": step.keyframe_jpeg,
                "keyframeWidth": step.keyframe_width,
                "keyframeHeight": step.keyframe_height,
                "transcriptText": step.transcript_text,
                "captionText": step.caption_text,
                "visualAction": step.visual_action,
            }
            for step in steps
        ],
        "fullTranscriptText": " ".join(t.text for t in transcripts).strip(),
        "fullCaptionText": "\n".join(c.text for c in captions).strip(),
    }


# Public introspection -------------------------------------------------------


def get_youtube_video_info(url: str) -> dict[str, Any]:
    """Return a compact summary of a YouTube URL without starting a session."""
    metadata = _run_ytdlp_metadata(url)
    video_format = _select_video_format(metadata)
    audio_format = _select_audio_format(metadata)
    caption_languages = sorted(
        set((metadata.get("subtitles") or {}).keys())
        | set((metadata.get("automatic_captions") or {}).keys())
    )
    return {
        "url": url,
        "video": _summarize_metadata(metadata),
        "playable": bool(video_format),
        "selectedVideoFormat": (
            {
                "formatId": video_format.get("format_id"),
                "ext": video_format.get("ext"),
                "protocol": video_format.get("protocol"),
                "height": video_format.get("height"),
                "fps": video_format.get("fps"),
            }
            if video_format
            else None
        ),
        "hasAudioOnlyFormat": bool(audio_format),
        "captionLanguages": caption_languages,
    }


def live_video_status() -> dict[str, Any]:
    """Report environment readiness for the live video lane."""
    ffmpeg_path = shutil.which("ffmpeg")
    yt_dlp_available, yt_dlp_version = _ytdlp_library_info()
    transcription_ready, transcription_reason = _whisper_available()
    with _sessions_lock:
        session_count = len(_sessions)
    return {
        "ffmpegAvailable": bool(ffmpeg_path),
        "ffmpegPath": ffmpeg_path,
        "ytDlpAvailable": yt_dlp_available,
        "ytDlpVersion": yt_dlp_version,
        "transcriptionAvailable": transcription_ready,
        "transcriptionUnavailableReason": transcription_reason if not transcription_ready else None,
        "activeSessions": session_count,
        "limits": {
            "maxFps": MAX_FPS,
            "maxFrameWidth": MAX_FRAME_WIDTH,
            "maxRingSize": MAX_FRAME_RING_SIZE,
            "maxPollFrames": MAX_POLL_FRAMES,
            "maxPollWaitSeconds": MAX_POLL_WAIT_SECONDS,
            "minAudioChunkSeconds": MIN_AUDIO_CHUNK_SECONDS,
            "minCaptionPollSeconds": MIN_CAPTION_POLL_SECONDS,
        },
    }


def _reset_all_sessions_for_tests() -> None:
    """Test-only hook: tear down every session synchronously."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        _shutdown_session(session)
