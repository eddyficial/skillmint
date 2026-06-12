"""Offline batch capture: download a YouTube VOD and process it at decode speed.

The live_video lane samples in real time — a 4-hour course takes 4 hours of wall
clock to watch through. For agents that just want a complete capture of a long
tutorial, this module is the batch counterpart: yt-dlp downloads the whole video
to a temp file, ffmpeg decodes that local file at native speed (typically 5–20×
faster than real-time for a typical VOD), keyframe diff + step events are computed
in one pass, captions are fetched once and bound to step intervals by video
timestamp, and the result is persisted straight to a tutorial playbook.

Single blocking call. Returns when the playbook is on disk. Intended use:

    capture_youtube_video_to_playbook(
        "https://youtu.be/HEmQky5umNQ",
        name="day-trading-full-course",
        fps=1.0,
        frame_width=480,
        summary="3h47m Jooviers Gems course",
    )

For a 4-hour 480p video this takes ~10–20 minutes total (network-bound download
+ decode-speed processing) instead of ~4 hours of real-time sampling.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from typing import Any

from . import tutorial_playbooks
from .live_video import (
    CAPTION_FETCH_TIMEOUT_SECONDS,
    JPEG_EOI,
    JPEG_SOI,
    LiveVideoError,
    MAX_FPS,
    MAX_FRAME_WIDTH,
    _compute_frame_thumbnail,
    _http_get_text,
    _parse_caption_cues,
    _read_jpeg_dimensions,
    _require_executable,
    _run_ytdlp_metadata,
    _select_caption_tracks,
    _summarize_metadata,
    _thumbnail_mean_abs_diff,
)
from .visual_actions import analyze_visual_action

LOCAL_CAPTION_MAX_BYTES = 16 * 1024 * 1024
WHISPER_DEFAULT_MODEL = "base"
WHISPER_DEFAULT_DEVICE = "auto"  # "auto" | "cuda" | "cpu"

DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 1800.0
DEFAULT_PROCESS_TIMEOUT_SECONDS = 1800.0
MAX_OFFLINE_TIMEOUT_SECONDS = 7200.0
DEFAULT_MAX_HEIGHT = 480


def capture_youtube_video_to_playbook(
    url: str,
    name: str,
    *,
    fps: float = 1.0,
    frame_width: int = 480,
    keyframe_diff_threshold: float = 12.0,
    min_step_seconds: float = 1.5,
    caption_languages: tuple[str, ...] = ("en",),
    overwrite: bool = False,
    summary: str | None = None,
    max_height: int = DEFAULT_MAX_HEIGHT,
    download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    process_timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    transcribe: bool = True,
    whisper_model: str = WHISPER_DEFAULT_MODEL,
    whisper_device: str = WHISPER_DEFAULT_DEVICE,
) -> dict[str, Any]:
    """Download a YouTube VOD, process every keyframe + caption, save as a playbook.

    Blocks until the playbook is on disk. For a typical 4-hour course at fps=1
    and frame_width=480, expect ~10–20 minutes total (network + decode). Live
    streams are rejected — use start_youtube_watch for those. Returns the
    persisted playbook's manifest.
    """
    fps = max(0.1, min(float(fps), MAX_FPS))
    frame_width = max(64, min(int(frame_width), MAX_FRAME_WIDTH))
    download_timeout_seconds = max(30.0, min(float(download_timeout_seconds), MAX_OFFLINE_TIMEOUT_SECONDS))
    process_timeout_seconds = max(30.0, min(float(process_timeout_seconds), MAX_OFFLINE_TIMEOUT_SECONDS))

    metadata = _run_ytdlp_metadata(url)
    if metadata.get("is_live"):
        raise LiveVideoError(
            "capture_youtube_video_to_playbook is for VODs only; "
            "live streams must use start_youtube_watch."
        )

    summary_meta = _summarize_metadata(metadata)
    caption_cues_by_lang: dict[str, list[dict[str, Any]]] = {}
    caption_errors: dict[str, str] = {}
    whisper_meta: dict[str, Any] | None = None
    transcribe_seconds = 0.0
    transcription_attempted = False
    for lang, track_url in _select_caption_tracks(metadata, caption_languages).items():
        try:
            body = _http_get_text(track_url, timeout_seconds=CAPTION_FETCH_TIMEOUT_SECONDS)
            caption_cues_by_lang[lang] = _parse_caption_cues(body)
        except LiveVideoError as exc:
            caption_errors[lang] = str(exc)
            caption_cues_by_lang[lang] = []

    with tempfile.TemporaryDirectory(prefix="skillmint-offline-capture-") as tmpdir:
        download_started = time.monotonic()
        local_path = _download_with_ytdlp(
            url, tmpdir, max_height=max_height, timeout_seconds=download_timeout_seconds
        )
        download_duration = time.monotonic() - download_started

        if transcribe and not any(caption_cues_by_lang.values()):
            language = caption_languages[0] if caption_languages else "en"
            transcription_attempted = True
            transcribe_started = time.monotonic()
            try:
                cues, whisper_meta = _transcribe_audio_to_cues(
                    local_path,
                    model_name=whisper_model,
                    device=whisper_device,
                    language=language,
                )
                caption_cues_by_lang[language] = cues
            except LiveVideoError as exc:
                caption_errors[language] = f"transcribe failed: {exc}"
                caption_cues_by_lang[language] = []
            transcribe_seconds = time.monotonic() - transcribe_started

        process_started = time.monotonic()
        steps = _process_local_video(
            local_path,
            fps=fps,
            frame_width=frame_width,
            keyframe_diff_threshold=keyframe_diff_threshold,
            min_step_seconds=min_step_seconds,
            caption_cues_by_lang=caption_cues_by_lang,
            timeout_seconds=process_timeout_seconds,
        )
        process_duration = time.monotonic() - process_started

    if not steps:
        raise LiveVideoError(
            "offline capture produced no step events; the keyframe threshold may be too high "
            "or the video is too short to decode any frames."
        )

    snapshot = {
        "sessionId": None,
        "url": url,
        "video": summary_meta,
        "config": {
            "fps": fps,
            "frameWidth": frame_width,
            "isLive": False,
            "keyframeDiffThreshold": keyframe_diff_threshold,
            "minStepSeconds": min_step_seconds,
            "captureMode": "offline_batch",
            "captionLanguagesRequested": list(caption_languages),
            "captionLanguagesResolved": [
                lang for lang, cues in caption_cues_by_lang.items() if cues
            ],
            "maxHeight": max_height,
            "downloadSeconds": round(download_duration, 2),
            "processSeconds": round(process_duration, 2),
            "transcribe": transcription_attempted,
            "transcribeSeconds": round(transcribe_seconds, 2),
            "whisper": whisper_meta,
        },
        "steps": steps,
    }
    result = tutorial_playbooks.persist_playbook_from_snapshot(
        name,
        snapshot,
        overwrite=overwrite,
        summary=summary,
    )
    result["downloadSeconds"] = round(download_duration, 2)
    result["processSeconds"] = round(process_duration, 2)
    result["transcribeSeconds"] = round(transcribe_seconds, 2)
    result["stepCount"] = len(steps)
    result["captionErrors"] = caption_errors
    result["whisper"] = whisper_meta
    return result


def capture_local_video_to_playbook(
    local_path: str,
    name: str,
    *,
    fps: float = 1.0,
    frame_width: int = 480,
    keyframe_diff_threshold: float = 12.0,
    min_step_seconds: float = 1.5,
    captions_path: str | None = None,
    caption_language: str = "en",
    transcribe: bool = True,
    whisper_model: str = WHISPER_DEFAULT_MODEL,
    whisper_device: str = WHISPER_DEFAULT_DEVICE,
    overwrite: bool = False,
    summary: str | None = None,
    process_timeout_seconds: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Decode a local video file and persist keyframe step events as a tutorial playbook.

    Mirror of capture_youtube_video_to_playbook for files already on disk — no yt-dlp
    download step. Captions come from one of: an explicit captions_path (VTT/SRT/json3),
    automatic transcription via faster-whisper when transcribe=True and no sidecar,
    or none at all (transcribe=False with no captions_path). Returns the persisted manifest.
    """
    if not os.path.isfile(local_path):
        raise LiveVideoError(f"local video path not found or not a file: {local_path}")

    fps = max(0.1, min(float(fps), MAX_FPS))
    frame_width = max(64, min(int(frame_width), MAX_FRAME_WIDTH))
    process_timeout_seconds = max(
        30.0, min(float(process_timeout_seconds), MAX_OFFLINE_TIMEOUT_SECONDS)
    )

    caption_cues_by_lang: dict[str, list[dict[str, Any]]] = {}
    caption_errors: dict[str, str] = {}
    whisper_meta: dict[str, Any] | None = None
    transcribe_seconds = 0.0
    if captions_path:
        if not os.path.isfile(captions_path):
            raise LiveVideoError(f"captions_path not found or not a file: {captions_path}")
        try:
            with open(captions_path, "rb") as fh:
                raw = fh.read(LOCAL_CAPTION_MAX_BYTES)
            body = raw.decode("utf-8", errors="replace")
            caption_cues_by_lang[caption_language] = _parse_caption_cues(body)
        except OSError as exc:
            caption_errors[caption_language] = f"read failed: {exc}"
            caption_cues_by_lang[caption_language] = []
    elif transcribe:
        transcribe_started = time.monotonic()
        try:
            cues, whisper_meta = _transcribe_audio_to_cues(
                local_path,
                model_name=whisper_model,
                device=whisper_device,
                language=caption_language if caption_language else None,
            )
            caption_cues_by_lang[caption_language] = cues
        except LiveVideoError as exc:
            caption_errors[caption_language] = f"transcribe failed: {exc}"
            caption_cues_by_lang[caption_language] = []
        transcribe_seconds = time.monotonic() - transcribe_started

    file_stat = os.stat(local_path)
    summary_meta: dict[str, Any] = {
        "id": None,
        "title": os.path.basename(local_path),
        "uploader": None,
        "channel": None,
        "isLive": False,
        "wasLive": False,
        "liveStatus": None,
        "durationSeconds": None,
        "viewCount": None,
        "concurrentViewCount": None,
        "uploadDate": None,
        "webpageUrl": None,
        "thumbnail": None,
        "availableCaptionLanguages": (
            [caption_language] if caption_cues_by_lang.get(caption_language) else []
        ),
        "hasAutomaticCaptions": False,
        "localPath": os.path.abspath(local_path),
        "fileSizeBytes": file_stat.st_size,
    }

    process_started = time.monotonic()
    steps = _process_local_video(
        local_path,
        fps=fps,
        frame_width=frame_width,
        keyframe_diff_threshold=keyframe_diff_threshold,
        min_step_seconds=min_step_seconds,
        caption_cues_by_lang=caption_cues_by_lang,
        timeout_seconds=process_timeout_seconds,
    )
    process_duration = time.monotonic() - process_started

    if not steps:
        raise LiveVideoError(
            "local video capture produced no step events; the keyframe threshold may be "
            "too high or the video is too short to decode any frames."
        )

    snapshot = {
        "sessionId": None,
        "url": None,
        "video": summary_meta,
        "config": {
            "fps": fps,
            "frameWidth": frame_width,
            "isLive": False,
            "keyframeDiffThreshold": keyframe_diff_threshold,
            "minStepSeconds": min_step_seconds,
            "captureMode": "offline_local_file",
            "captionLanguagesRequested": [caption_language] if captions_path else [],
            "captionLanguagesResolved": [
                lang for lang, cues in caption_cues_by_lang.items() if cues
            ],
            "localPath": os.path.abspath(local_path),
            "captionsPath": os.path.abspath(captions_path) if captions_path else None,
            "processSeconds": round(process_duration, 2),
            "transcribe": bool(transcribe and not captions_path),
            "transcribeSeconds": round(transcribe_seconds, 2),
            "whisper": whisper_meta,
        },
        "steps": steps,
    }
    result = tutorial_playbooks.persist_playbook_from_snapshot(
        name,
        snapshot,
        overwrite=overwrite,
        summary=summary,
    )
    result["processSeconds"] = round(process_duration, 2)
    result["transcribeSeconds"] = round(transcribe_seconds, 2)
    result["stepCount"] = len(steps)
    result["captionErrors"] = caption_errors
    result["whisper"] = whisper_meta
    return result


_nvidia_dlls_loaded = False


def _register_nvidia_dll_dirs() -> None:
    """Make pip-installed nvidia-cublas / nvidia-cudnn DLLs discoverable for CTranslate2.

    The nvidia-cublas-cu12 and nvidia-cudnn-cu12 wheels install DLLs under
    site-packages/nvidia/*/bin, which is NOT on Windows' default DLL search path.
    os.add_dll_directory alone is not sufficient because CTranslate2's loader does
    not pick up added directories for indirect deps, so we also force-preload the
    cuBLAS DLL into the process via ctypes.WinDLL — once loaded, CTranslate2 finds
    it by name during the first inference call.
    """
    global _nvidia_dlls_loaded
    if _nvidia_dlls_loaded or not hasattr(os, "add_dll_directory"):
        return
    try:
        import ctypes
        import importlib.util

        for pkg, sub in (("nvidia.cublas", "bin"), ("nvidia.cudnn", "bin")):
            spec = importlib.util.find_spec(pkg)
            if spec is None or not spec.submodule_search_locations:
                continue
            dll_dir = os.path.join(spec.submodule_search_locations[0], sub)
            if not os.path.isdir(dll_dir):
                continue
            os.add_dll_directory(dll_dir)
            for entry in os.listdir(dll_dir):
                if entry.lower().endswith(".dll"):
                    try:
                        ctypes.WinDLL(os.path.join(dll_dir, entry))
                    except OSError:
                        pass  # missing transitive dep, keep trying others
        _nvidia_dlls_loaded = True
    except Exception:
        pass  # best-effort; CPU fallback still works


def _transcribe_audio_to_cues(
    local_path: str,
    *,
    model_name: str = WHISPER_DEFAULT_MODEL,
    device: str = WHISPER_DEFAULT_DEVICE,
    language: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Transcribe a local video's audio via faster-whisper, return VTT-shaped cues + metadata.

    Auto-detects CUDA → float16; falls back to CPU → int8. The caller binds the cues to
    keyframe windows via _captions_in_window, same as sidecar captions.
    """
    _register_nvidia_dll_dirs()
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise LiveVideoError(
            "faster-whisper is required for auto-transcription but is not installed."
        ) from exc

    actual_device, compute_type = _resolve_whisper_device(device)
    try:
        model = WhisperModel(model_name, device=actual_device, compute_type=compute_type)
    except Exception as exc:
        if actual_device == "cuda":
            try:
                model = WhisperModel(model_name, device="cpu", compute_type="int8")
                actual_device, compute_type = "cpu", "int8"
            except Exception as fallback_exc:
                raise LiveVideoError(
                    f"faster-whisper failed on both cuda and cpu: {exc} / {fallback_exc}"
                ) from fallback_exc
        else:
            raise LiveVideoError(f"faster-whisper init failed: {exc}") from exc

    try:
        segments, info = model.transcribe(
            local_path,
            language=language,
            vad_filter=True,
        )
        cues: list[dict[str, Any]] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            cues.append(
                {
                    "startSeconds": float(seg.start),
                    "endSeconds": float(seg.end),
                    "text": text,
                }
            )
    except Exception as exc:
        raise LiveVideoError(f"faster-whisper transcription failed: {exc}") from exc

    meta = {
        "model": model_name,
        "device": actual_device,
        "computeType": compute_type,
        "detectedLanguage": getattr(info, "language", None),
        "languageProbability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "audioDurationSeconds": float(getattr(info, "duration", 0.0) or 0.0),
        "cueCount": len(cues),
    }
    return cues, meta


def _resolve_whisper_device(device: str) -> tuple[str, str]:
    """Pick (device, compute_type) from a user request. 'auto' probes CUDA first."""
    if device == "cpu":
        return "cpu", "int8"
    if device == "cuda":
        return "cuda", "float16"
    if device == "auto":
        try:
            _register_nvidia_dll_dirs()
            from faster_whisper import WhisperModel
            probe = WhisperModel("tiny", device="cuda", compute_type="float16")
            # Probe actual inference (not just model load) so we catch the cuBLAS
            # DLL-missing case before committing the full transcription run.
            import numpy as np
            silence = np.zeros(16000, dtype=np.float32)
            list(probe.transcribe(silence, language="en", vad_filter=False)[0])
            del probe
            return "cuda", "float16"
        except Exception:
            return "cpu", "int8"
    raise LiveVideoError(f"whisper_device must be 'auto', 'cuda', or 'cpu' — got {device!r}")


def _download_with_ytdlp(
    url: str,
    tmpdir: str,
    *,
    max_height: int,
    timeout_seconds: float,
) -> str:
    """Pull the best video+audio to a single mp4 under tmpdir using yt-dlp."""
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise LiveVideoError(
            "yt-dlp Python package is required for offline capture but is not installed."
        ) from exc

    outpath_template = os.path.join(tmpdir, "video.%(ext)s")
    opts = {
        "format": (
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}][ext=mp4]/"
            f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best"
        ),
        "outtmpl": outpath_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "merge_output_format": "mp4",
        "retries": 5,
        "fragment_retries": 5,
    }
    deadline = time.monotonic() + timeout_seconds
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadError as exc:
        raise LiveVideoError(f"yt-dlp download failed for {url}: {exc}") from exc
    except Exception as exc:
        raise LiveVideoError(f"yt-dlp download failed for {url}: {exc}") from exc
    if time.monotonic() > deadline:
        raise LiveVideoError(f"yt-dlp download exceeded timeout of {timeout_seconds:.0f}s")

    candidates = [
        os.path.join(tmpdir, name)
        for name in os.listdir(tmpdir)
        if not name.endswith(".part") and not name.endswith(".ytdl")
    ]
    if not candidates:
        raise LiveVideoError("yt-dlp produced no output file")
    # If yt-dlp wrote separate video+audio plus a merged file, prefer the largest.
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return candidates[0]


def _process_local_video(
    local_path: str,
    *,
    fps: float,
    frame_width: int,
    keyframe_diff_threshold: float,
    min_step_seconds: float,
    caption_cues_by_lang: dict[str, list[dict[str, Any]]],
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Decode the local video, run keyframe diff, emit one step record per keyframe.

    Each step carries a videoStartSeconds / videoEndSeconds window that lets us
    pull the matching caption cues for that interval.
    """
    ffmpeg = _require_executable("ffmpeg")
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        local_path,
        "-an",
        "-sn",
        "-vf",
        f"fps={fps},scale={frame_width}:-2",
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
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    deadline = time.monotonic() + timeout_seconds
    steps: list[dict[str, Any]] = []
    last_keyframe_thumb: bytes | None = None
    last_step_jpeg: bytes | None = None
    frame_count = 0
    last_keyframe_video_time = -float("inf")
    last_step_end_video_time = 0.0

    try:
        if process.stdout is None:
            raise LiveVideoError("ffmpeg failed to open stdout pipe")
        buffer = bytearray()
        chunk_size = 64 * 1024
        while True:
            if time.monotonic() > deadline:
                raise LiveVideoError(
                    f"offline ffmpeg decode exceeded timeout of {timeout_seconds:.0f}s"
                )
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
                frame_count += 1
                video_time = (frame_count - 1) / fps
                try:
                    width, height = _read_jpeg_dimensions(jpeg_bytes)
                except Exception:
                    width = frame_width
                    height = 0
                try:
                    thumb = _compute_frame_thumbnail(jpeg_bytes)
                except Exception:
                    continue
                is_keyframe = False
                diff_score = 0.0
                if last_keyframe_thumb is None:
                    is_keyframe = True
                    diff_score = float("inf")
                else:
                    diff_score = _thumbnail_mean_abs_diff(last_keyframe_thumb, thumb)
                    if diff_score >= keyframe_diff_threshold:
                        is_keyframe = True
                if not is_keyframe:
                    continue
                if (
                    steps
                    and (video_time - last_keyframe_video_time) < min_step_seconds
                ):
                    last_keyframe_thumb = thumb
                    continue
                step_start = last_step_end_video_time
                step_end = video_time
                caption_text = _captions_in_window(
                    caption_cues_by_lang, step_start, step_end
                )
                visual_action = analyze_visual_action(
                    last_step_jpeg,
                    jpeg_bytes,
                    diff_score=(
                        diff_score
                        if diff_score != float("inf")
                        else None
                    ),
                    video_time_seconds=video_time,
                )
                steps.append(
                    {
                        "sequence": len(steps) + 1,
                        "startedAt": step_start,
                        "endedAt": step_end,
                        "videoStartSeconds": step_start,
                        "videoEndSeconds": step_end,
                        "trigger": "keyframe",
                        "diffScore": (
                            diff_score
                            if diff_score != float("inf")
                            else None
                        ),
                        "secondsSincePrevious": step_end - step_start,
                        "keyframeJpeg": jpeg_bytes,
                        "keyframeWidth": width,
                        "keyframeHeight": height,
                        "transcriptText": "",
                        "captionText": caption_text,
                        "visualAction": visual_action,
                    }
                )
                last_step_jpeg = jpeg_bytes
                last_keyframe_thumb = thumb
                last_keyframe_video_time = video_time
                last_step_end_video_time = video_time
    finally:
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    if process.returncode not in (0, None) and not steps:
        stderr = b""
        if process.stderr is not None:
            try:
                stderr = process.stderr.read() or b""
            except OSError:
                stderr = b""
        raise LiveVideoError(
            f"ffmpeg decode failed (exit {process.returncode}): "
            f"{stderr.decode('utf-8', errors='replace').strip() or 'no stderr'}"
        )
    return steps


def _captions_in_window(
    caption_cues_by_lang: dict[str, list[dict[str, Any]]],
    start_seconds: float,
    end_seconds: float,
) -> str:
    """Collect caption text whose startSeconds falls within [start, end] across languages."""
    chunks: list[str] = []
    for cues in caption_cues_by_lang.values():
        for cue in cues:
            cue_start = cue.get("startSeconds")
            if not isinstance(cue_start, (int, float)):
                continue
            if cue_start < start_seconds or cue_start >= end_seconds:
                continue
            text = (cue.get("text") or "").strip()
            if text:
                chunks.append(text)
    return "\n".join(chunks)
