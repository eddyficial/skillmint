"""Thin subprocess wrapper around the Claude Code non-interactive CLI (`claude -p`).

Shared by any skillmint module that needs to invoke a Claude session
programmatically — primarily skill_validation. Keeps the Anthropic SDK out of
skillmint's dependency tree; usage stays inside the user's existing Claude Code
subscription.

The CLI is invoked once per call. Stdout is the response, stderr is captured,
exit code is propagated. There is no streaming, no retries, no rate-limit
handling — callers that need those build them on top.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass


class ClaudeCliError(RuntimeError):
    """Raised when the `claude` CLI is missing, errored, or timed out."""


@dataclass
class ClaudeCliResult:
    stdout: str
    stderr: str
    exit_code: int
    wall_seconds: float
    cwd: str


def ensure_available() -> str:
    """Return the resolved path to `claude` on PATH, or raise ClaudeCliError.

    Cheap to call repeatedly — uses shutil.which.
    """
    path = shutil.which("claude")
    if path:
        return path
    raise ClaudeCliError(
        "claude CLI not found on PATH. Install Claude Code "
        "(https://claude.com/claude-code) and ensure `claude` is callable."
    )


def run(
    prompt: str,
    *,
    cwd: str | None = None,
    timeout_seconds: float = 300.0,
    extra_args: list[str] | None = None,
) -> ClaudeCliResult:
    """Invoke `claude -p` once with ``prompt`` on stdin and return the result.

    ``cwd`` becomes the spawned process's working directory (and is what the
    Claude session sees for Bash/Edit/Read paths). ``extra_args`` is forwarded
    verbatim before the prompt flag for future flag expansion. The prompt is
    passed on stdin, not as a command-line argument — Windows caps a process's
    total command-line length (~8K-32K chars depending on shell/quoting), and
    prompts embedding full source lessons routinely exceed that, failing with
    "The command line is too long." `claude -p` reads the prompt from stdin
    when no positional prompt argument is given.

    Raises ClaudeCliError on missing CLI, timeout, or process spawn failure.
    A non-zero exit code does NOT raise — callers inspect ``exit_code`` so they
    can distinguish "claude exited cleanly with an unhappy report" from "claude
    crashed."
    """
    exe = ensure_available()
    args: list[str] = [exe, "-p"]
    if extra_args:
        args.extend(extra_args)

    resolved_cwd = cwd if cwd is not None else os.getcwd()
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 — explicit exe path, no shell
            args,
            input=prompt,
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        raise ClaudeCliError(
            f"claude -p timed out after {timeout_seconds:.0f}s "
            f"(actual wall: {elapsed:.1f}s, cwd={resolved_cwd})"
        ) from exc
    except (OSError, FileNotFoundError) as exc:
        raise ClaudeCliError(f"failed to spawn claude: {exc}") from exc

    elapsed = time.monotonic() - started
    return ClaudeCliResult(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        exit_code=int(completed.returncode),
        wall_seconds=round(elapsed, 2),
        cwd=resolved_cwd,
    )
