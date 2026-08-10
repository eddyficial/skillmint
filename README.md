# SkillMint

Source in. Certified skill out.

SkillMint is an open-source capability compiler for agent systems. It turns source material such as videos, SOPs, PDFs, web pages, and documentation into traceable playbooks, generated agent skills, and machine-readable trust artifacts.

The goal is not to summarize content. The goal is to compile operational knowledge into reusable, inspectable, testable agent capabilities.

## Current Status

SkillMint is alpha software, but the core local workflow is usable today for controlled sources:

- owned material
- licensed material
- internal SOPs and documentation
- public-domain material
- material where you can attest permission or fair-use review

The local GUI now enforces the safer path: generated GUI skills must be finalized, validated, and certification-gated. The GUI also requires a rights basis before it starts a creation job.

Do not treat generated capabilities as production-ready for high-risk domains until the certification artifacts pass and you have reviewed the generated skill.

## What It Creates

For each source, SkillMint can produce:

- a normalized playbook
- distilled lessons
- a generated skill
- target exports for Claude Code, Codex, Cursor, Windsurf, or Markdown
- `capability.json`
- `evidence.json`
- `certification.json`
- rights and provenance assessment
- prompt-injection assessment
- audit ledger entry
- local capability registry entry

The playbook is used as the source-of-truth during generation. In the GUI, keeping the playbook on disk is optional.

## How It Works

```text
source
  -> capture
  -> playbook
  -> distill
  -> prompt-injection scan
  -> scaffold
  -> codify
  -> export
  -> validate
  -> certify
```

1. **Capture**

   SkillMint captures a YouTube video, local video, PDF, web page, or documentation site into a normalized playbook. Videos are segmented into timed steps with captions and optional keyframes. Written sources become structured text sections.

2. **Distill**

   Raw steps are grouped into lessons. This produces `lessons.md` and `lessons.json`.

3. **Guard**

   Captured source text is scanned for prompt injection before any skill is created. If the source tries to tell SkillMint, Codex, Claude, or an agent to create a different skill, ignore instructions, read secrets, call tools, or change roles, creation stops.

4. **Scaffold And Codify**

   SkillMint composes a scaffold and finalizes it into an executable skill. Deterministic codification is the default and does not require an AI provider. Claude CLI codification is optional.

5. **Validate And Certify**

   Validation executes the generated skill against its success criteria through the local Claude CLI. Certification combines validation results with source fidelity, evidence bindings, codification status, rights governance, prompt-injection screening, visual grounding, and validator coverage.

## Install

SkillMint is Windows-first and targets Python 3.11+.

```powershell
git clone https://github.com/eddyficial/skillmint.git
cd skillmint
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,video-transcription]"
```

Required for video capture:

- `ffmpeg` on `PATH`
- `yt-dlp`, installed through Python dependencies

Quick checks:

```powershell
ffmpeg -version
python -m yt_dlp --version
```

On Windows, one common `ffmpeg` install path is:

```powershell
winget install Gyan.FFmpeg
```

`winget` updates your PATH permanently, but the terminal you ran it from will
not see the new PATH until you close and reopen it. Open a **fresh** terminal
before running `ffmpeg -version` to confirm the install — running it in the
same window will still report "not found."

Required for GUI-certified creation and certified CLI commands:

- Claude Code CLI on `PATH`, because validation uses `claude -p`

Quick check:

```powershell
claude --version
```

Optional extras:

```powershell
python -m pip install -e ".[rendered-web]"
python -m playwright install chromium
python -m pip install -e ".[ocr]"
```

Use `rendered-web` for JavaScript-rendered pages. Use `ocr` only when working with scanned PDFs and local OCR tooling.

## Use The Local GUI

Start the workbench:

```powershell
skillmint-ui --open
```

or:

```powershell
.\.venv\Scripts\python.exe -m skillmint.web_ui --open
```

Then, in the **Create** panel:

1. Paste a source URL or local file path.
2. Select a **rights basis** (required — the GUI won't submit without one).
3. Set **Export intent** if you're not keeping this private/local — it feeds the same rights gate as `--export-intent` on the CLI and can block a public/commercial export outright.
4. Leave **Source type** on `Auto` unless detection guesses wrong (e.g. a local video file misread as something else).
5. Pick the **export target** (Claude, Codex, Cursor, Markdown, or Windsurf).
6. Leave **Keep playbook**, **Overwrite existing asset** off, and **Same-origin crawl** at their defaults unless you know you need otherwise.
7. Click **Create skill**.

**Codifier choice matters here too.** Open **Source controls** and check **Finalize provider** before you click Create. It defaults to `Deterministic` — free, no AI call, but the output is often too generic to pass validation. Switch it to `Claude CLI` once you're past a first smoke test; see the note under [Use The CLI](#use-the-cli) for why.

**Source controls** also holds everything else you won't need for a first run but will eventually: `Summary` / `Scope notes` / `Trigger phrase` / `Owner agent` to hand-tune the generated skill's framing, `Skills root` to target a different project, `Source owner` / `Source license` for the rights assessment, `Validation timeout`, doc-crawl options (`URL pattern`, `Max pages`, `Fetch timeout`), PDF page range, and video options (`Video FPS`, `Frame width`, `Captions path`, `Caption languages`, and whether to auto-transcribe when captions are missing).

**After you click Create skill**, the job runs in the background — expect 30+ seconds, since the GUI's certified path always validates through a real `claude -p` call. The **Build output** panel on the right tracks it live through four stages (Capture → Distill → Compose → Export), then shows the result: paths to the generated `SKILL.md`, the playbook, lessons, and the export sidecar (each with a **Copy** button), plus the validation pass/fail summary. **Recent jobs** and **Playbooks** below it list everything you've created so far in this session.

The GUI always creates a skill through the certified path:

- finalization is on
- validation is on
- certification is required
- rights basis is required
- prompt-injection screening is enforced

The GUI currently creates `Skill` assets only. Agent and workflow scaffolds still exist in lower-level APIs, but they are not exposed in the certified GUI path because execution validation currently supports skills.

## Use The CLI

The examples below use placeholders such as `<source-url>`. Replace them with source material you own, have licensed, or can otherwise use.

**Codifier choice matters.** Every example below uses `--codify-provider deterministic` (the default) unless it explicitly sets `--codify-provider claude_cli`. Deterministic codification just restates the distilled source in list form — free, no AI call, good for a first smoke test — but the result is often too generic to pass `--validate`. `claude_cli` codification spends one real Claude Code call synthesizing an actual grounded procedure and is what produces a skill worth keeping. Once a source captures cleanly, add `--codify-provider claude_cli` to the command before you rely on the output.

For a local smoke source, start a tiny web server from the repo:

```powershell
python -m http.server 8123 --bind 127.0.0.1 --directory examples
```

Then use:

```text
http://127.0.0.1:8123/quickstart-sop.html
```

Stop the example server with `Ctrl+C` when you are done.

Create a certified Markdown skill from a web page:

```powershell
skillmint-create "http://127.0.0.1:8123/quickstart-sop.html" `
  --target markdown `
  --rights-basis owned `
  --validate `
  --require-certification
```

Create a certified Codex skill from a YouTube video:

```powershell
skillmint-create "<youtube-url>" `
  --target codex `
  --rights-basis user_attested_permission `
  --source-owner "Creator or channel name" `
  --validate `
  --require-certification
```

Create a certified skill from a local PDF:

```powershell
skillmint-create "C:\docs\internal-sop.pdf" `
  --source-type pdf `
  --target claude_code `
  --rights-basis internal `
  --validate `
  --require-certification
```

Crawl a documentation site:

```powershell
skillmint-create "<docs-site-url>" `
  --source-type documentation_site `
  --max-pages 25 `
  --rights-basis licensed `
  --validate `
  --require-certification
```

Use Claude CLI for richer codification:

```powershell
skillmint-create "<source-url>" `
  --rights-basis owned `
  --codify-provider claude_cli `
  --validate `
  --require-certification
```

The CLI still exposes lower-level development switches such as `--no-codify`. Do not use those for user-facing or shareable capabilities.

## Rights Basis Values

Use one of:

- `owned`
- `licensed`
- `internal`
- `user_attested_permission`
- `creative_commons`
- `public_domain`
- `fair_use`

Public or commercial export can be blocked when the rights assessment says the source is not safe for that intent.

## Outputs

Playbooks are stored under:

```text
~/.skillmint/playbooks/<slug>/
```

Override the playbook store:

```powershell
$env:SKILLMINT_PLAYBOOK_DIR = "C:\path\to\playbooks"
```

Typical playbook layout:

```text
~/.skillmint/playbooks/<slug>/
  manifest.json
  steps.json
  transcript.md
  lessons.md
  lessons.json
  keyframes/
```

Project-local trust artifacts are stored under:

```text
<project>/.skillmint/
  capabilities/<skill-slug>/
    capability.json
    evidence.json
    certification.json
  audit/
    capability-ledger.jsonl
  registry/
    capabilities.json
```

Target exports:

```text
Claude Code:  <project>/.claude/skills/<slug>/SKILL.md
Codex:        <project>/.agents/skills/<slug>/SKILL.md
Cursor:       <project>/.cursor/rules/<slug>.mdc
Windsurf:     <project>/.windsurf/rules/<slug>.md
Markdown:     <project>/.skillmint/exports/markdown/<slug>.md
```

Each export also gets a `skillmint.json` or `.skillmint.json` sidecar where that target supports it.

## Safety Gates

SkillMint includes deterministic gates for:

- source prompt injection
- rights and provenance
- source fidelity
- evidence bindings
- codification completion
- execution validation
- certification scoring

The prompt-injection gate runs before scaffold or export. The rights gate runs before export. Certification records the result of both.

## MCP Server

Run the MCP server:

```powershell
skillmint
```

Example MCP config:

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

Recommended tool:

```text
create_skill_from_source(source, skill_name=None, source_type="auto", ...)
```

## Troubleshooting

**The GUI rejects my job with "rights basis required".**

Select a rights basis before creating the skill. This is intentional.

**Validation fails because Claude CLI is unavailable.**

Install Claude Code CLI and make sure `claude` is on `PATH`, then restart the GUI.

**YouTube capture fails.**

Confirm `ffmpeg` is on `PATH`, update `yt-dlp`, and try a video with captions first.

**The prompt-injection guard blocks creation.**

The source contains text that appears to target SkillMint, Codex, Claude, an agent, tools, shell commands, secrets, or system instructions. Use a different source or manually review the material.

**A public export is blocked.**

Use private/internal export, or provide a stronger rights basis such as `owned`, `licensed`, or `public_domain`.

## Development

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Current local result:

```text
211 passed, 1 skipped
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, PR expectations, and the MCP-wrapper-sync rule before making changes.

## License

MIT. See [LICENSE](LICENSE).
