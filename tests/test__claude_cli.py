"""Tests for skillmint._claude_cli.

These mock subprocess.run so they run offline and don't require a real
`claude` binary on PATH. They exist to pin down how the prompt reaches the
`claude -p` process — see the regression tests below for the bugs they guard
against.
"""
from __future__ import annotations

from typing import Any

import pytest

from skillmint import _claude_cli


class _Completed:
    """Minimal completed-process stand-in mirroring subprocess.run's return shape."""

    def __init__(self, stdout: str = "ok", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _stub_which(monkeypatch: pytest.MonkeyPatch, path: str = "/fake/claude") -> None:
    monkeypatch.setattr(_claude_cli.shutil, "which", lambda name: path)


def test_run_passes_prompt_via_stdin_not_as_a_cli_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the prompt used to be appended to argv, which blew past
    Windows's command-line length cap for prompts embedding full source
    lessons ("The command line is too long."). It must go over stdin.
    """
    _stub_which(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _Completed:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(_claude_cli.subprocess, "run", fake_run)

    prompt = "x" * 50_000  # far beyond any OS argv length limit
    _claude_cli.run(prompt, cwd=".")

    assert prompt not in captured["args"]
    assert all(len(str(a)) < len(prompt) for a in captured["args"])
    assert captured["kwargs"]["input"] == prompt


def test_run_forces_utf8_with_replace_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: without an explicit encoding, subprocess.run falls back to
    the Windows locale encoding (cp1252/"charmap") for stdin, which raises
    UnicodeEncodeError on prompts containing characters like '→'. Forcing
    UTF-8 with errors="replace" keeps the call from crashing on such input.
    """
    _stub_which(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _Completed:
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(_claude_cli.subprocess, "run", fake_run)

    _claude_cli.run("capture -> playbook -> distill → codify", cwd=".")

    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["text"] is True


def test_run_builds_args_without_a_trailing_prompt_positional(monkeypatch: pytest.MonkeyPatch) -> None:
    """argv should just be [claude, -p, *extra_args] — no positional prompt."""
    _stub_which(monkeypatch, path="/fake/claude")
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> _Completed:
        captured["args"] = args
        return _Completed()

    monkeypatch.setattr(_claude_cli.subprocess, "run", fake_run)

    _claude_cli.run("hello", cwd=".", extra_args=["--output-format", "text"])

    assert captured["args"] == ["/fake/claude", "-p", "--output-format", "text"]


def test_run_returns_stdout_and_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_which(monkeypatch)
    monkeypatch.setattr(
        _claude_cli.subprocess,
        "run",
        lambda *a, **kw: _Completed(stdout="PONG", stderr="", returncode=0),
    )

    result = _claude_cli.run("ping", cwd=".")

    assert result.stdout == "PONG"
    assert result.exit_code == 0
