"""FastMCP server surface for Skillmint.

Registers every tool that lives in the skillmint.* modules so a Claude Code
(or any MCP) client can invoke them as mcp__skillmint__<tool>. The server is
launched via ``python -m skillmint`` (see __main__.py).

Naming: each function below mirrors the underlying module function 1:1 with a
``_tool`` suffix. The wrapper exists to (a) handle exceptions and (b) shape
the response into the str/Image-list types FastMCP expects.

House rule (from feedback_periphery_tool_wrappers, carried over): when you add
a new kwarg to a function in skillmint/<module>.py, ALSO add it here in the
matching ``*_tool`` wrapper — otherwise MCP clients silently see the old
behavior. Two-file change, always.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP, Image

from skillmint.live_video import (
    LiveVideoError,
    fetch_youtube_captions,
    follow_youtube_tutorial,
    get_youtube_video_info,
    list_youtube_watches,
    live_video_status,
    poll_youtube_watch,
    start_youtube_watch,
    stop_youtube_watch,
    youtube_frame_snapshot,
    youtube_watch_status,
)
from skillmint.offline_video_capture import capture_youtube_video_to_playbook
from skillmint.document_capture import (
    capture_documentation_site_to_playbook,
    capture_pdf_to_playbook,
    capture_web_page_to_playbook,
)
from skillmint.skill_synthesis import (
    SkillSynthesisError,
    compose_skill_scaffold_from_playbook,
)
from skillmint.tutorial_playbooks import (
    TutorialPlaybookError,
    delete_tutorial_playbook,
    distill_tutorial_playbook,
    list_tutorial_playbooks,
    read_tutorial_playbook,
    rename_tutorial_playbook,
    save_tutorial_as_playbook,
)


mcp = FastMCP("skillmint")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _json_payload(payload: object) -> str:
    """Serialize a payload for MCP responses. Bytes fields are base64-encoded upstream."""
    return json.dumps(payload, indent=2, default=_json_default)


def _json_default(value: object) -> object:
    if isinstance(value, (bytes, bytearray)):
        import base64
        return base64.b64encode(bytes(value)).decode("ascii")
    raise TypeError(f"object of type {type(value).__name__} is not JSON-serializable")


def _live_video_error_payload(exc: LiveVideoError) -> str:
    return _json_payload({"ok": False, "error": str(exc), "errorType": "LiveVideoError"})


def _tutorial_playbook_error_payload(exc: TutorialPlaybookError) -> str:
    return _json_payload({"ok": False, "error": str(exc), "errorType": "TutorialPlaybookError"})


def _skill_synthesis_error_payload(exc: SkillSynthesisError) -> str:
    return _json_payload({"ok": False, "error": str(exc), "errorType": "SkillSynthesisError"})


# ---------------------------------------------------------------------------
# Capture + inspect
# ---------------------------------------------------------------------------


@mcp.tool(
    name="live_video_status",
    description="Report ffmpeg, yt-dlp, and Whisper transcription readiness for the live video lane, plus active session count and limits.",
)
def live_video_status_tool() -> str:
    return _json_payload(live_video_status())


@mcp.tool(
    name="get_youtube_video_info",
    description="Resolve a YouTube URL via yt-dlp and return compact metadata: title, channel, isLive, duration, available caption languages, and whether a playable video format exists.",
)
def get_youtube_video_info_tool(url: str) -> str:
    try:
        return _json_payload(get_youtube_video_info(url))
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)


@mcp.tool(
    name="youtube_frame_snapshot",
    description="Grab a single JPEG frame from a YouTube URL via ffmpeg. For live streams the latest frame is returned. Use the watch session tools when you need a continuous stream of frames. Pass timeout_seconds (default 60, max 300) when targeting a multi-hour video whose HLS cache is cold.",
    structured_output=False,
)
def youtube_frame_snapshot_tool(
    url: str,
    at_seconds: float | None = None,
    frame_width: int = 640,
    quality: int = 5,
    timeout_seconds: float | None = None,
) -> list[object]:
    try:
        result = youtube_frame_snapshot(
            url,
            at_seconds=at_seconds,
            frame_width=frame_width,
            quality=quality,
            timeout_seconds=timeout_seconds,
        )
    except LiveVideoError as exc:
        return [_live_video_error_payload(exc)]
    metadata = {
        "url": result["url"],
        "video": result["video"],
        "atSeconds": result["atSeconds"],
        "frameWidth": result["frameWidth"],
        "quality": result["quality"],
        "byteLength": len(result["jpegBytes"]),
        "isLive": result["isLive"],
    }
    return [
        Image(data=result["jpegBytes"], format="jpeg"),
        json.dumps(metadata, indent=2),
    ]


@mcp.tool(
    name="youtube_captions",
    description="One-shot caption fetch for a YouTube URL. Returns per-language cue lists where each cue is {startSeconds, endSeconds, text}. ALWAYS pass max_cues (e.g. 50) when targeting a long video — without it, a multi-hour course returns thousands of cues and will blow the MCP tool result budget. The response includes cueCount and a truncated flag so the agent knows what was discarded.",
)
def youtube_captions_tool(
    url: str,
    languages: list[str] | None = None,
    max_cues: int | None = None,
) -> str:
    selected = tuple(languages) if languages else ("en",)
    try:
        return _json_payload(fetch_youtube_captions(url, languages=selected, max_cues=max_cues))
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)


# ---------------------------------------------------------------------------
# Real-time watch sessions
# ---------------------------------------------------------------------------


@mcp.tool(
    name="start_youtube_watch",
    description="Start a long-running YouTube watch session that samples frames at fps, optionally transcribes audio (requires faster-whisper installed), and optionally polls auto-caption tracks. Live streams default to low-latency mode. Returns a sessionId to use with poll_youtube_watch and stop_youtube_watch.",
)
def start_youtube_watch_tool(
    url: str,
    fps: float = 2.0,
    frame_width: int = 640,
    include_audio: bool = False,
    include_captions: bool = True,
    caption_languages: list[str] | None = None,
    audio_chunk_seconds: float = 4.0,
    ring_size: int = 60,
    caption_poll_seconds: float = 3.0,
    low_latency: bool | None = None,
    keyframe_diff_threshold: float = 12.0,
    min_step_seconds: float = 1.5,
    quiet_step_seconds: float = 8.0,
) -> str:
    languages = tuple(caption_languages) if caption_languages else ("en",)
    try:
        result = start_youtube_watch(
            url,
            fps=fps,
            frame_width=frame_width,
            include_audio=include_audio,
            include_captions=include_captions,
            caption_languages=languages,
            audio_chunk_seconds=audio_chunk_seconds,
            ring_size=ring_size,
            caption_poll_seconds=caption_poll_seconds,
            low_latency=low_latency,
            keyframe_diff_threshold=keyframe_diff_threshold,
            min_step_seconds=min_step_seconds,
            quiet_step_seconds=quiet_step_seconds,
        )
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)
    return _json_payload(result)


@mcp.tool(
    name="poll_youtube_watch",
    description="Return new frames, audio transcripts, and caption deltas for a YouTube watch session since the supplied sequence numbers. Set wait_seconds to long-poll until new data arrives or the timeout elapses. Frames are JPEG bytes base64-encoded under jpegBase64.",
)
def poll_youtube_watch_tool(
    session_id: str,
    since_frame_sequence: int | None = None,
    since_transcript_sequence: int | None = None,
    since_caption_sequence: int | None = None,
    max_frames: int = 5,
    wait_seconds: float = 0.0,
    include_frame_bytes: bool = True,
) -> str:
    try:
        result = poll_youtube_watch(
            session_id,
            since_frame_sequence=since_frame_sequence,
            since_transcript_sequence=since_transcript_sequence,
            since_caption_sequence=since_caption_sequence,
            max_frames=max_frames,
            wait_seconds=wait_seconds,
            include_frame_bytes=include_frame_bytes,
        )
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)
    return _json_payload(result)


@mcp.tool(
    name="stop_youtube_watch",
    description="Stop a YouTube watch session and tear down its ffmpeg processes and worker threads.",
)
def stop_youtube_watch_tool(session_id: str) -> str:
    try:
        return _json_payload(stop_youtube_watch(session_id))
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)


@mcp.tool(
    name="list_youtube_watches",
    description="Return a snapshot of every active YouTube watch session.",
)
def list_youtube_watches_tool() -> str:
    return _json_payload(list_youtube_watches())


@mcp.tool(
    name="youtube_watch_status",
    description="Return the current status of one YouTube watch session without modifying it.",
)
def youtube_watch_status_tool(session_id: str) -> str:
    try:
        return _json_payload(youtube_watch_status(session_id))
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)


@mcp.tool(
    name="follow_youtube_tutorial",
    description="Long-poll the next step events for an agent following a YouTube tutorial. Each step bundles one keyframe (the visual moment that changed) plus all transcript and caption text since the previous step. Use this instead of poll_youtube_watch when you want the agent to react to logical tutorial steps rather than redundant frame-by-frame polling.",
)
def follow_youtube_tutorial_tool(
    session_id: str,
    since_step_sequence: int | None = None,
    max_steps: int = 4,
    wait_seconds: float = 0.0,
    include_keyframe_bytes: bool = True,
) -> str:
    try:
        result = follow_youtube_tutorial(
            session_id,
            since_step_sequence=since_step_sequence,
            max_steps=max_steps,
            wait_seconds=wait_seconds,
            include_keyframe_bytes=include_keyframe_bytes,
        )
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)
    return _json_payload(result)


# ---------------------------------------------------------------------------
# Playbook lifecycle
# ---------------------------------------------------------------------------


@mcp.tool(
    name="capture_youtube_video_to_playbook",
    description=(
        "Offline batch counterpart to start_youtube_watch: download a YouTube VOD via yt-dlp, "
        "decode it through ffmpeg at full speed (typically 5-20x faster than real-time), and "
        "persist the full set of keyframe step events to a named tutorial playbook in one call. "
        "Use this when you want a complete capture of a long course without sampling in real "
        "time (a 4-hour video typically lands in ~10-20 minutes instead of 4 hours). Rejects "
        "live streams (those must use start_youtube_watch). Blocks until the playbook is saved."
    ),
)
def capture_youtube_video_to_playbook_tool(
    url: str,
    name: str,
    fps: float = 1.0,
    frame_width: int = 480,
    keyframe_diff_threshold: float = 12.0,
    min_step_seconds: float = 1.5,
    caption_languages: list[str] | None = None,
    overwrite: bool = False,
    summary: str | None = None,
    max_height: int = 480,
    download_timeout_seconds: float = 1800.0,
    process_timeout_seconds: float = 1800.0,
) -> str:
    langs = tuple(caption_languages) if caption_languages else ("en",)
    try:
        return _json_payload(
            capture_youtube_video_to_playbook(
                url,
                name,
                fps=fps,
                frame_width=frame_width,
                keyframe_diff_threshold=keyframe_diff_threshold,
                min_step_seconds=min_step_seconds,
                caption_languages=langs,
                overwrite=overwrite,
                summary=summary,
                max_height=max_height,
                download_timeout_seconds=download_timeout_seconds,
                process_timeout_seconds=process_timeout_seconds,
            )
        )
    except LiveVideoError as exc:
        return _live_video_error_payload(exc)
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="save_tutorial_as_playbook",
    description="Persist the current follow-along session's accumulated step events as a named tutorial playbook on disk. The agent can call read_tutorial_playbook(name) in any future session to recall the steps without re-watching the video. Optional summary is a model-written prose recap of what the tutorial taught.",
)
def save_tutorial_as_playbook_tool(
    session_id: str,
    name: str,
    overwrite: bool = False,
    summary: str | None = None,
    max_steps: int | None = None,
) -> str:
    try:
        return _json_payload(
            save_tutorial_as_playbook(
                session_id,
                name,
                overwrite=overwrite,
                summary=summary,
                max_steps=max_steps,
            )
        )
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="list_tutorial_playbooks",
    description="List every saved tutorial playbook with name, source URL, step count, and summary.",
)
def list_tutorial_playbooks_tool() -> str:
    return _json_payload(list_tutorial_playbooks())


@mcp.tool(
    name="read_tutorial_playbook",
    description="Load a saved tutorial playbook by name. With include_keyframes=true, returns base64-encoded JPEGs for the most recent max_keyframes steps so the agent can re-view what was demonstrated. Defaults to metadata + transcript text only for cheap recall.",
)
def read_tutorial_playbook_tool(
    name: str,
    include_keyframes: bool = False,
    max_keyframes: int | None = None,
) -> str:
    try:
        return _json_payload(
            read_tutorial_playbook(
                name,
                include_keyframes=include_keyframes,
                max_keyframes=max_keyframes,
            )
        )
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="delete_tutorial_playbook",
    description="Delete a saved tutorial playbook directory and all of its keyframes.",
)
def delete_tutorial_playbook_tool(name: str) -> str:
    try:
        return _json_payload(delete_tutorial_playbook(name))
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="rename_tutorial_playbook",
    description=(
        "Rename a saved tutorial playbook to a short, memorable topic label so users can actually "
        "type its name later. Captured playbooks default to verbose video-title-derived names; "
        "rename to topic labels like 'day-trading-futures', 'vercel-setup', 'powerbi-dashboards'. "
        "Moves the directory and updates the manifest, lessons JSON, and markdown H1 to match. "
        "Pass overwrite=true to replace an existing playbook of the new name."
    ),
)
def rename_tutorial_playbook_tool(
    old_name: str,
    new_name: str,
    overwrite: bool = False,
) -> str:
    try:
        return _json_payload(rename_tutorial_playbook(old_name, new_name, overwrite=overwrite))
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="distill_tutorial_playbook",
    description=(
        "Strip karaoke noise from a saved tutorial playbook and group its steps into topical sections. "
        "Writes lessons.md and lessons.json next to the existing manifest/steps — pure mechanical cleanup, no LLM. "
        "This is what /study loads: the distilled output is ~10x cheaper to read than the raw caption fragments "
        "and is what an agent actually needs to apply what the tutorial taught. Section breaks fire on quiet "
        "periods or hard visual cuts (diffScore >= section_diff_score, default 60)."
    ),
)
def distill_tutorial_playbook_tool(
    name: str,
    section_diff_score: float = 60.0,
) -> str:
    try:
        return _json_payload(distill_tutorial_playbook(name, section_diff_score=section_diff_score))
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


# ---------------------------------------------------------------------------
# Skill synthesis
# ---------------------------------------------------------------------------


@mcp.tool(
    name="compose_skill_scaffold_from_playbook",
    description=(
        "Compose a Claude Code scaffold from a saved tutorial playbook. The output is either a "
        "single SKILL.md at .claude/skills/<slug>/ (one procedure, default for short captures) or "
        "an orchestrating agent .md at .claude/agents/<slug>.md (a role spanning many skills, "
        "auto-selected for curriculum-shaped captures like 'X Bootcamp' / 'Full Course'). The "
        "scaffold lists every distilled section as a bullet and stubs out a codify block for the "
        "current Claude session to fill in via /codify. Pass shape='skill' or shape='agent' to "
        "override the heuristic. Pass overwrite=true to replace an existing scaffold of the same "
        "name. Pass scope_notes to attach author-supplied constraints to the source-notes block."
    ),
)
def compose_skill_scaffold_from_playbook_tool(
    playbook_name: str,
    skill_name: str,
    shape: str = "auto",
    trigger_description: str | None = None,
    scope_notes: str | None = None,
    overwrite: bool = False,
    skills_root: str | None = None,
) -> str:
    try:
        return _json_payload(
            compose_skill_scaffold_from_playbook(
                playbook_name,
                skill_name,
                shape=shape,
                trigger_description=trigger_description,
                scope_notes=scope_notes,
                overwrite=overwrite,
                skills_root=skills_root,
            )
        )
    except SkillSynthesisError as exc:
        return _skill_synthesis_error_payload(exc)


# ---------------------------------------------------------------------------
# Document capture (HTML pages, PDFs, multi-page doc sites)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="capture_web_page_to_playbook",
    description=(
        "Fetch a single HTML page, strip nav/footer/script noise, extract the main content, "
        "split it into heading-based sections, and persist as a Skillmint playbook. Use for "
        "setup guides, single-page references, blog tutorials, and other one-URL training "
        "material. The resulting playbook is consumed by distill_tutorial_playbook -> "
        "compose_skill_scaffold_from_playbook -> /codify exactly like a YouTube playbook."
    ),
)
def capture_web_page_to_playbook_tool(
    url: str,
    name: str,
    summary: str | None = None,
    overwrite: bool = False,
    timeout_seconds: float = 30.0,
) -> str:
    try:
        return _json_payload(
            capture_web_page_to_playbook(
                url=url,
                name=name,
                summary=summary,
                overwrite=overwrite,
                timeout_seconds=timeout_seconds,
            )
        )
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="capture_pdf_to_playbook",
    description=(
        "Extract text from a local PDF (vendor whitepaper, API reference, ebook) and persist "
        "as a Skillmint playbook with one step per page. Pass page_range as [start, end] to "
        "slice long PDFs. Does NOT OCR scanned images; if no text comes out, the PDF is "
        "image-only and needs OCR (not implemented). After capture, run distill_tutorial_playbook "
        "to re-group by topic."
    ),
)
def capture_pdf_to_playbook_tool(
    path: str,
    name: str,
    summary: str | None = None,
    overwrite: bool = False,
    page_range: list[int] | None = None,
) -> str:
    if page_range is not None and len(page_range) != 2:
        return _tutorial_playbook_error_payload(
            TutorialPlaybookError("page_range must be a 2-element list [start, end]")
        )
    pr = tuple(page_range) if page_range else None
    try:
        return _json_payload(
            capture_pdf_to_playbook(
                path=path,
                name=name,
                summary=summary,
                overwrite=overwrite,
                page_range=pr,
            )
        )
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


@mcp.tool(
    name="capture_documentation_site_to_playbook",
    description=(
        "BFS-crawl a documentation site starting at a seed URL up to max_pages, following "
        "same-origin links inside the main content area. Each fetched page becomes one or more "
        "steps (split by heading). Use for multi-page API docs (docs.anthropic.com, "
        "learn.microsoft.com, etc.). Pass url_pattern (substring or regex) to constrain which "
        "links are followed -- e.g. r'/docs/' to skip blog/marketing pages. Static HTML only; "
        "JS-rendered sites will return their skeleton, not the rendered content."
    ),
)
def capture_documentation_site_to_playbook_tool(
    url: str,
    name: str,
    summary: str | None = None,
    overwrite: bool = False,
    max_pages: int = 30,
    same_origin_only: bool = True,
    url_pattern: str | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    try:
        return _json_payload(
            capture_documentation_site_to_playbook(
                url=url,
                name=name,
                summary=summary,
                overwrite=overwrite,
                max_pages=max_pages,
                same_origin_only=same_origin_only,
                url_pattern=url_pattern,
                timeout_seconds=timeout_seconds,
            )
        )
    except TutorialPlaybookError as exc:
        return _tutorial_playbook_error_payload(exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Skillmint MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
