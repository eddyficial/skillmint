# Periscribe

Capture, distill, and codify external knowledge into reusable Claude Code skills.

Periscribe is the **learning loop** in the Periphery family — sibling to [Periphery](https://periphery.ai) (Windows desktop MCP) and the [PeriCode](https://pericode.dev) family (CLI / Inside / Sidecar agent distributions).

Where Periphery lets an agent **see and act on** a Windows desktop, Periscribe lets an agent **learn from** external content (YouTube tutorials, PDFs, web docs) and turn that knowledge into permanent capabilities (Claude Code skills, playbooks, trained models).

## What it does

Periscribe is an MCP server. It exposes tools that move along this pipeline:

```
YouTube URL / PDF / doc
        ↓
   capture_youtube_video_to_playbook   →  ~/.periscribe/playbooks/<slug>/
        ↓
   distill_tutorial_playbook            →  lessons.md, lessons.json (cleaned, sectioned)
        ↓
   compose_skill_scaffold_from_playbook →  .claude/skills/<slug>/SKILL.md (scaffold)
        ↓
   /codify slash command                →  .claude/skills/<slug>/SKILL.md (procedure filled in)
        ↓
   Any future Claude Code session auto-loads the skill on matching user phrasing
```

The agent that started the day knowing nothing about Vercel / Power BI / nanoGPT / your project's setup can — after one Periscribe pipeline run — walk a user through doing that thing, citing video timestamps when claims are grounded.

## Tools

The MCP server registers these tools (all under the `mcp__periscribe__` prefix):

**Capture / inspect**
- `capture_youtube_video_to_playbook(url, name, ...)` — offline-batch capture: yt-dlp downloads, ffmpeg decodes at native speed, keyframe diff produces step events, captions bound per-step by video timestamp. ~10–20 min for a 4-hour course on a typical machine.
- `youtube_frame_snapshot(url, at_seconds, ...)` — one-shot frame pull
- `youtube_captions(url, max_cues, ...)` — per-cue caption fetch
- `get_youtube_video_info(url)` — metadata only
- `live_video_status()` — readiness check (ffmpeg, yt-dlp, faster-whisper)

**Real-time follow-along**
- `start_youtube_watch / poll_youtube_watch / stop_youtube_watch` — long-running watch session
- `follow_youtube_tutorial(session_id, ...)` — step-event polling (collapses frame firehose to logical moments)
- `list_youtube_watches`, `youtube_watch_status`

**Playbook lifecycle**
- `save_tutorial_as_playbook(session_id, name)` — persist a live watch session
- `list_tutorial_playbooks()`, `read_tutorial_playbook(name)`, `delete_tutorial_playbook(name)`
- `rename_tutorial_playbook(old_name, new_name)` — short topic names so users can recall
- `distill_tutorial_playbook(name)` — strip karaoke noise, group into topical sections

**Skill synthesis**
- `compose_skill_scaffold_from_playbook(playbook_name, skill_name, ...)` — writes `.claude/skills/<slug>/SKILL.md` scaffold; current Claude session finishes the codification via the `/codify` skill

## Install

```powershell
# Clone + venv
git clone https://github.com/eddyficial/Periscribe.git
cd Periscribe
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[video-transcription]
```

System deps:
- **Python 3.11+**
- **ffmpeg** on PATH (Windows: `winget install ffmpeg` or [gyan.dev/ffmpeg](https://www.gyan.dev/ffmpeg/builds/))
- yt-dlp installed via the pip dep (used as a Python library, not a subprocess — no PATH gymnastics needed)

Register with your MCP client (Claude Code / Cursor / etc) by adding to `.mcp.json`:

```json
{
  "mcpServers": {
    "periscribe": {
      "type": "stdio",
      "command": "C:\\path\\to\\Periscribe\\.venv\\Scripts\\python.exe",
      "args": ["-m", "periscribe"],
      "cwd": "C:\\path\\to\\Periscribe"
    }
  }
}
```

## Storage

Playbooks land at `~/.periscribe/playbooks/<slug>/` by default. Override with `PERISCRIBE_PLAYBOOK_DIR`.

Each playbook is a directory:
```
~/.periscribe/playbooks/<slug>/
├── manifest.json          (name, source URL, video metadata, step count, summary)
├── steps.json             (ordered step records with timestamps, captions, keyframe paths)
├── lessons.md             (cleaned prose, sectioned — after distill)
├── lessons.json           (structured sections — after distill)
├── transcript.md          (human-readable narration with embedded keyframes)
└── keyframes/
    ├── 001.jpg
    ├── 002.jpg
    └── ...
```

## House style

- **Windows-first**, Python 3.11+. Cross-platform support is a future goal, not a current contract.
- **No human-control bypass.** A failed high-level action must not silently fall back to blind raw input. Surface the blocker.
- **MCP tool wrappers must mirror the underlying function signature.** Adding a kwarg in `periscribe/<module>.py` requires updating the matching `*_tool` wrapper in `periscribe/server.py`, or MCP clients silently see old behavior.

## Sibling projects

- **[Periphery](https://periphery.ai)** — Windows MCP server for desktop / window / screen / input automation. The "do" to Periscribe's "learn."
- **PeriCode** — agent distributions: CLI, Inside (in-process SDK), Sidecar (external UIA).

## License

MIT.
