# Skillmint

Capture, distill, and codify external knowledge into reusable Claude Code skills.

Skillmint is the **learning loop** in the Periphery family — sibling to [Periphery](https://periphery.ai) (Windows desktop MCP) and the [PeriCode](https://pericode.dev) family (CLI / Inside / Sidecar agent distributions).

Where Periphery lets an agent **see and act on** a Windows desktop, Skillmint lets an agent **learn from** external content (YouTube tutorials, PDFs, web docs) and turn that knowledge into permanent capabilities (Claude Code skills, playbooks, trained models).

## What it does

Skillmint is an MCP server. It exposes tools that move along this pipeline:

```
YouTube URL  ─┐
HTML page    ─┤
PDF file     ─┼──→  capture_*_to_playbook   →  ~/.skillmint/playbooks/<slug>/
Docs site    ─┘                                 (manifest, steps, transcript, optional keyframes)
                          ↓
                 distill_tutorial_playbook   →  lessons.md, lessons.json (cleaned, sectioned)
                          ↓
              compose_skill_scaffold_from_playbook
                                              →  .claude/skills/<slug>/SKILL.md (scaffold)
                          ↓
                  /codify slash command       →  .claude/skills/<slug>/SKILL.md (procedure filled in)
                          ↓
                  Any future Claude Code session auto-loads the skill on matching user phrasing
```

Four source types, one downstream pipeline. The agent that started the day knowing nothing about Vercel / Power BI / your vendor's API can — after one Skillmint pipeline run — walk a user through doing that thing, citing the source section / page / video timestamp when claims are grounded.

## Tools

The MCP server registers these tools (all under the `mcp__skillmint__` prefix):

**Capture — YouTube**
- `capture_youtube_video_to_playbook(url, name, ...)` — offline-batch capture: yt-dlp downloads, ffmpeg decodes at native speed (~100×+ real-time), keyframe diff produces step events, captions bound per-step by video timestamp. A 4-hour course typically lands in 1–2 minutes.
- `youtube_frame_snapshot(url, at_seconds, ...)` — one-shot frame pull
- `youtube_captions(url, max_cues, ...)` — per-cue caption fetch
- `get_youtube_video_info(url)` — metadata only
- `live_video_status()` — readiness check (ffmpeg, yt-dlp, faster-whisper)

**Capture — written material**
- `capture_web_page_to_playbook(url, name, ...)` — fetch a single HTML page, strip nav/footer/script noise, extract main content, split into heading-based sections. For setup guides, blog tutorials, single-page references. JS-rendered sites return their skeleton; export to PDF as workaround.
- `capture_pdf_to_playbook(path, name, page_range=None, ...)` — extract text from a local PDF, one step per page. Pass `page_range=[start, end]` for long PDFs. Does not OCR scanned image-only PDFs.
- `capture_documentation_site_to_playbook(url, name, max_pages=30, url_pattern=None, ...)` — BFS-crawl a docs site from a seed URL, following same-origin links inside main content. For multi-page API docs (`docs.anthropic.com`, `learn.microsoft.com`). Use `url_pattern=r"/docs/"` or similar to constrain scope.

**Real-time follow-along (YouTube only)**
- `start_youtube_watch / poll_youtube_watch / stop_youtube_watch` — long-running watch session
- `follow_youtube_tutorial(session_id, ...)` — step-event polling (collapses frame firehose to logical moments)
- `list_youtube_watches`, `youtube_watch_status`

**Playbook lifecycle (source-agnostic)**
- `save_tutorial_as_playbook(session_id, name)` — persist a live watch session
- `list_tutorial_playbooks()`, `read_tutorial_playbook(name)`, `delete_tutorial_playbook(name)`
- `rename_tutorial_playbook(old_name, new_name)` — short topic names so users can recall
- `distill_tutorial_playbook(name)` — strip karaoke noise, group into topical sections; works on any source

**Skill synthesis (source-agnostic)**
- `compose_skill_scaffold_from_playbook(playbook_name, skill_name, ...)` — writes `.claude/skills/<slug>/SKILL.md` scaffold; current Claude session finishes the codification via the `/codify` skill

## Install

```powershell
# Clone + venv
git clone https://github.com/eddyficial/skillmint.git
cd skillmint
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[video-transcription]
```

System deps:
- **Python 3.11+**
- **ffmpeg** on PATH for YouTube capture (Windows: `winget install ffmpeg` or [gyan.dev/ffmpeg](https://www.gyan.dev/ffmpeg/builds/)). Not needed for web/PDF/docs capture.
- yt-dlp, httpx, lxml, pdfplumber installed via pip deps (no extra system setup).
- Optional `[video-transcription]` extra adds faster-whisper for audio transcription when captions are missing.

Register with your MCP client (Claude Code / Cursor / etc) by adding to `.mcp.json`:

```json
{
  "mcpServers": {
    "skillmint": {
      "type": "stdio",
      "command": "C:\\path\\to\\skillmint\\.venv\\Scripts\\python.exe",
      "args": ["-m", "skillmint"],
      "cwd": "C:\\path\\to\\skillmint"
    }
  }
}
```

## Storage

Playbooks land at `~/.skillmint/playbooks/<slug>/` by default. Override with `SKILLMINT_PLAYBOOK_DIR`.

Each playbook is a directory. Layout for video sources:
```
~/.skillmint/playbooks/<slug>/
├── manifest.json          (name, source URL, metadata, step count, summary, sourceKind)
├── steps.json             (ordered step records with timestamps, captions, keyframe paths)
├── lessons.md             (cleaned prose, sectioned — after distill)
├── lessons.json           (structured sections — after distill)
├── transcript.md          (human-readable narration with embedded keyframes)
└── keyframes/             (only present for video sources)
    ├── 001.jpg
    └── ...
```

For HTML / PDF / docs-site sources the `keyframes/` directory is omitted and `transcript.md` is text-only. Everything downstream (distill, scaffold, codify) reads the same structure regardless of source.

## House style

- **Windows-first**, Python 3.11+. Cross-platform support is a future goal, not a current contract.
- **No human-control bypass.** A failed high-level action must not silently fall back to blind raw input. Surface the blocker.
- **MCP tool wrappers must mirror the underlying function signature.** Adding a kwarg in `skillmint/<module>.py` requires updating the matching `*_tool` wrapper in `skillmint/server.py`, or MCP clients silently see old behavior.

## Sibling projects

- **[Periphery](https://periphery.ai)** — Windows MCP server for desktop / window / screen / input automation. The "do" to Skillmint's "learn."
- **PeriCode** — agent distributions: CLI, Inside (in-process SDK), Sidecar (external UIA).

## License

MIT.
