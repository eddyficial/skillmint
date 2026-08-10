# Contributing to SkillMint

SkillMint is alpha software. Contributions are welcome, but the certification
and safety-gate logic (rights, prompt-injection, execution validation) is the
core value of the project — changes there get more scrutiny than changes
elsewhere.

## Dev setup

```powershell
git clone https://github.com/eddyficial/skillmint.git
cd skillmint
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,video-transcription]"
```

See the [README](README.md#install) for optional extras (`rendered-web`,
`ocr`) and the `ffmpeg` / Claude Code CLI prerequisites needed for actually
running captures — the automated test suite below does not require either.

## Running tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite mocks every network and subprocess boundary (yt-dlp, ffmpeg,
`claude -p`, HTTP fetches), so it runs fully offline and doesn't need
`ffmpeg` or the Claude Code CLI installed. CI runs the same command on
Windows for Python 3.11 and 3.12 — see `.github/workflows/ci.yml`.

For an end-to-end sanity check against real sources (not part of CI),
`scripts/smoke_test_e2e.py` runs capture → distill → compose against
YouTube, a web page, a local PDF, and a documentation site. Point its `pdf`
target at any local PDF path before running it — don't commit a real path
back.

## Before opening a PR

- Add or update tests for any behavior change. If you're fixing a bug,
  write a test that fails against the old code and passes against the fix —
  that's the standard this project holds itself to (see recent commits for
  examples).
- Run the full suite locally; CI will re-run it on Windows/3.11+3.12 either way.
- When you add or change a public function argument in any `skillmint/*.py`
  module, update the matching MCP wrapper in `skillmint/server.py` to match —
  MCP clients only see what the wrapper exposes, so a signature change that
  isn't mirrored there silently breaks the MCP surface.
- Don't commit hardcoded local file paths (`C:\Users\...`, `/home/...`) in
  scripts or examples — use a placeholder like `C:\path\to\...` instead.
- If a change touches `skillmint/rights.py`, `skillmint/prompt_injection.py`,
  or `skillmint/skill_validation.py`, explain the reasoning in the PR
  description — these are the safety gates and deserve more explanation than
  a one-line diff.

## Commit messages

This repo loosely follows Conventional Commits: `type: summary` or
`type(scope): summary`, imperative mood, no period. Common types in the
history: `feat`, `fix`, `test`, `docs`, `chore`, `rename`. A longer body
explaining *why* (not just what) is encouraged for anything non-trivial —
see `git log` for the house style.

## Reporting bugs

Open a GitHub issue with: what you ran (command or MCP tool call), what you
expected, what happened instead, and — if it's a capture/certification
issue — the relevant `certification.json` or `evidence.json` if you're able
to share it. If it involves rights or prompt-injection gating specifically,
say so explicitly; those get triaged first.

## License

By contributing, you agree your contributions are licensed under the
project's [MIT License](LICENSE).
