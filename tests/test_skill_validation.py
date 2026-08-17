"""Tests for skillmint.skill_validation.

These tests mock the `claude -p` subprocess so they run offline. The end-to-end
smoke test that actually spawns claude lives in scripts/smoke_validate_skill.py
and is documented in the validate-skill plan — not in this test file.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillmint import _claude_cli, skill_validation


# ---------------------------------------------------------------------------
# Frontmatter + criteria parsing
# ---------------------------------------------------------------------------


def test_parse_frontmatter_pulls_top_level_keys() -> None:
    text = (
        "---\n"
        "name: foo-skill\n"
        "description: Does foo.\n"
        "---\n\n"
        "# Body\n"
    )
    parsed = skill_validation._parse_frontmatter(text)
    assert parsed == {"name": "foo-skill", "description": "Does foo."}


def test_parse_frontmatter_handles_nested_inputs_mapping() -> None:
    text = (
        "---\n"
        "name: foo-skill\n"
        "inputs:\n"
        "  target_path: pathlib.Path (required) — where to write\n"
        "  greeting: str (default 'hello')\n"
        "---\n\n"
    )
    parsed = skill_validation._parse_frontmatter(text)
    assert "inputs" in parsed
    inputs = parsed["inputs"]
    assert isinstance(inputs, dict)
    assert "target_path" in inputs
    assert "greeting" in inputs


def test_parse_frontmatter_returns_empty_when_missing() -> None:
    text = "# No frontmatter here\n"
    assert skill_validation._parse_frontmatter(text) == {}


# ---------------------------------------------------------------------------
# '## Inputs' body-section parsing (canonical source since frontmatter
# dropped inputs/outputs/dependencies to match the official Agent Skills spec)
# ---------------------------------------------------------------------------


def test_parse_inputs_section_reads_typed_bullets_from_body() -> None:
    text = (
        "---\n"
        "name: foo-skill\n"
        "description: Does foo.\n"
        "---\n\n"
        "## Inputs\n\n"
        "- `target_path` (pathlib.Path, required): where to write the file.\n"
        "- `greeting` (string, optional): the text to write.\n\n"
        "## Outputs\n\n"
        "- `artifact`: the written file.\n"
    )
    schema = skill_validation._parse_inputs_section(text)
    assert schema == {
        "target_path": "pathlib.Path, required",
        "greeting": "string, optional",
    }


def test_parse_inputs_section_ignores_description_text_after_colon() -> None:
    """A description mentioning 'local paths' must not leak a path-typed default.

    The parenthetical, not the free-text description, is what's captured —
    this is what stops a plain string arg like `source_context` from being
    misread as a filesystem path just because its description says
    "local paths, versions, or environment details".
    """
    text = (
        "## Inputs\n\n"
        "- `source_context` (string, optional): Extra constraints, local paths, "
        "versions, or environment details.\n"
    )
    schema = skill_validation._parse_inputs_section(text)
    assert schema == {"source_context": "string, optional"}
    inputs = skill_validation._materialize_sample_inputs(schema)
    assert inputs["source_context"] != "<SANDBOX>/source_context"


def test_parse_inputs_section_returns_empty_when_missing() -> None:
    text = "---\nname: foo\n---\n\n## How to apply\n\nDo it.\n"
    assert skill_validation._parse_inputs_section(text) == {}


def test_parse_success_criteria_returns_bullets_in_order() -> None:
    text = (
        "## Success criteria\n\n"
        "- First check.\n"
        "- Second check with more detail.\n"
        "- Third check.\n\n"
        "## Failure modes\n"
        "- Not a criterion.\n"
    )
    criteria = skill_validation._parse_success_criteria(text)
    assert criteria == [
        "First check.",
        "Second check with more detail.",
        "Third check.",
    ]


def test_parse_success_criteria_empty_when_section_missing() -> None:
    text = "# No success criteria section\n"
    assert skill_validation._parse_success_criteria(text) == []


# ---------------------------------------------------------------------------
# Sample input materialization
# ---------------------------------------------------------------------------


def test_materialize_sample_inputs_picks_sandbox_placeholder_for_paths() -> None:
    schema = {
        "target_path": "pathlib.Path (required) — where to write",
        "greeting": "str (default 'hello')",
        "count": "int (default 3)",
        "enabled": "bool",
    }
    inputs = skill_validation._materialize_sample_inputs(schema)
    assert inputs["target_path"] == "<SANDBOX>/target_path"
    assert isinstance(inputs["greeting"], str)
    assert isinstance(inputs["count"], int)
    assert inputs["enabled"] is True


def test_materialize_sample_inputs_handles_literal() -> None:
    schema = {"mode": "Literal['scaffold', 'level', 'paddle']"}
    inputs = skill_validation._materialize_sample_inputs(schema)
    assert inputs["mode"] == "scaffold"


def test_resolve_sandbox_placeholders_swaps_in_real_paths(tmp_path: Path) -> None:
    inputs = {
        "target_path": "<SANDBOX>/out.txt",
        "greeting": "hello",
    }
    resolved = skill_validation._resolve_sandbox_placeholders(inputs, str(tmp_path))
    assert resolved["target_path"] == str(tmp_path / "out.txt")
    assert resolved["greeting"] == "hello"


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------


def test_parse_report_extracts_fenced_json_block() -> None:
    criteria = ["File exists", "File contains hello"]
    stdout = (
        "I ran the skill. Here is the report:\n"
        "```json\n"
        '{"criteria": ['
        '{"name": "File exists", "passed": true, "evidence": "out.txt found"},'
        '{"name": "File contains hello", "passed": false, "evidence": "got world"}'
        "]}\n"
        "```\n"
    )
    report = skill_validation._parse_report(stdout, criteria)
    assert len(report) == 2
    assert report[0]["passed"] is True
    assert report[0]["check"] == "File exists"
    assert report[1]["passed"] is False
    assert "got world" in (report[1]["evidence"] or "")


def test_parse_report_falls_back_to_regex_on_malformed_json() -> None:
    criteria = ["First", "Second"]
    stdout = (
        "I ran it.\n"
        "1. PASS - the first check worked\n"
        "2. FAIL - the second check failed\n"
    )
    report = skill_validation._parse_report(stdout, criteria)
    assert report[0]["passed"] is True
    assert report[1]["passed"] is False


def test_parse_report_marks_missing_criteria_as_failed() -> None:
    criteria = ["First", "Second", "Third"]
    stdout = (
        "```json\n"
        '{"criteria": ['
        '{"name": "First", "passed": true, "evidence": "ok"}'
        "]}\n"
        "```\n"
    )
    report = skill_validation._parse_report(stdout, criteria)
    assert len(report) == 3
    assert report[0]["passed"] is True
    assert report[1]["passed"] is False
    assert "not reported" in (report[1]["evidence"] or "")
    assert report[2]["passed"] is False


# ---------------------------------------------------------------------------
# End-to-end (mocked subprocess)
# ---------------------------------------------------------------------------


SAMPLE_SKILL = """---
name: echo-skill
description: Write a greeting to a file.
inputs:
  target_path: pathlib.Path (required) — where to write
  greeting: str (default 'hello')
outputs:
  artifact: pathlib.Path
---

# echo-skill

## How to apply
1. Use Write to create `target_path` with `greeting` as the content.

## Success criteria
- The file at `target_path` exists.
- The file's content is exactly the `greeting` value.
"""


def _make_fake_run(stdout: str, exit_code: int = 0):
    """Build a callable that mimics _claude_cli.run for monkeypatching."""

    def fake_run(prompt: str, *, cwd: str | None = None, timeout_seconds: float = 300.0,
                 extra_args=None):  # noqa: ANN001 - test helper
        return _claude_cli.ClaudeCliResult(
            stdout=stdout,
            stderr="",
            exit_code=exit_code,
            wall_seconds=0.1,
            cwd=cwd or "/tmp",
        )

    return fake_run


def test_validate_skill_returns_green_when_all_pass(monkeypatch, tmp_path: Path) -> None:
    """End-to-end with a mocked claude session that PASSes both criteria."""
    skill_dir = tmp_path / ".claude" / "skills" / "echo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SAMPLE_SKILL, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    fake_stdout = (
        "Done.\n"
        "```json\n"
        '{"criteria": ['
        '{"name": "exists", "passed": true, "evidence": "out.txt found"},'
        '{"name": "content", "passed": true, "evidence": "matches"}'
        "]}\n"
        "```\n"
    )
    monkeypatch.setattr(_claude_cli, "run", _make_fake_run(fake_stdout))
    # Also mock the ensure_available path used implicitly when _claude_cli.run is
    # not monkeypatched at module import time — defensive in case of refactor.
    monkeypatch.setattr(_claude_cli, "ensure_available", lambda: "/fake/claude")

    result = skill_validation.validate_skill("echo-skill")
    assert result["ok"] is True
    assert result["passed"] == 2
    assert result["failed"] == 0
    assert all(c["passed"] for c in result["criteria"])


def test_validate_skill_reports_failed_when_criterion_fails(monkeypatch, tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "echo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SAMPLE_SKILL, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    fake_stdout = (
        "```json\n"
        '{"criteria": ['
        '{"name": "exists", "passed": true, "evidence": "yes"},'
        '{"name": "content", "passed": false, "evidence": "wrong text"}'
        "]}\n"
        "```\n"
    )
    monkeypatch.setattr(_claude_cli, "run", _make_fake_run(fake_stdout))
    monkeypatch.setattr(_claude_cli, "ensure_available", lambda: "/fake/claude")

    result = skill_validation.validate_skill("echo-skill")
    assert result["ok"] is False
    assert result["passed"] == 1
    assert result["failed"] == 1


def test_validate_skill_errors_when_skill_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # Also redirect HOME so the global-skills fallback can't accidentally find one
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(skill_validation.SkillValidationError, match="not found"):
        skill_validation.validate_skill("nonexistent-skill")


BODY_ONLY_SKILL = """---
name: echo-skill-v2
description: Write a greeting to a file.
---

# echo-skill-v2

## Inputs

- `target_path` (pathlib.Path, required): where to write.
- `greeting` (string, optional): the text to write.

## How to apply
1. Use Write to create `target_path` with `greeting` as the content.

## Success criteria
- The file at `target_path` exists.
"""


def test_validate_skill_materializes_inputs_from_body_when_frontmatter_has_none(
    monkeypatch, tmp_path: Path
) -> None:
    """No `inputs:` in frontmatter (the new normal) still yields typed sample inputs.

    Confirms the fix actually preserves validate_skill's sample-input
    generation now that SkillMint-generated skills carry only name/
    description in frontmatter.
    """
    skill_dir = tmp_path / ".claude" / "skills" / "echo-skill-v2"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(BODY_ONLY_SKILL, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    captured: dict = {}

    def fake_run(prompt: str, *, cwd: str | None = None, timeout_seconds: float = 300.0, extra_args=None):
        captured["prompt"] = prompt
        return _claude_cli.ClaudeCliResult(
            stdout='```json\n{"criteria":[{"name":"exists","passed":true,"evidence":"ok"}]}\n```',
            stderr="",
            exit_code=0,
            wall_seconds=0.1,
            cwd=cwd or "/tmp",
        )

    monkeypatch.setattr(_claude_cli, "run", fake_run)
    monkeypatch.setattr(_claude_cli, "ensure_available", lambda: "/fake/claude")

    result = skill_validation.validate_skill("echo-skill-v2")
    assert result["ok"] is True
    # target_path resolved to a real sandbox path, not left as a placeholder
    # and not defaulted to the generic <sample-...> string.
    assert result["sampleInputs"]["target_path"].endswith("target_path")
    assert result["sampleInputs"]["greeting"] != "<SANDBOX>/greeting"


def test_validate_skill_resolves_supplied_skills_root(monkeypatch, tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "inputs:\n"
        "  request: string\n"
        "---\n\n"
        "## How to apply\n\n"
        "Do it.\n\n"
        "## Success criteria\n\n"
        "- It is done.\n",
        encoding="utf-8",
    )

    def fake_run(prompt: str, *, cwd: str | None = None, timeout_seconds: float = 300.0, extra_args=None):
        return _claude_cli.ClaudeCliResult(
            stdout='```json\n{"criteria":[{"name":"done","passed":true,"evidence":"ok"}]}\n```',
            stderr="",
            exit_code=0,
            wall_seconds=0.1,
            cwd=cwd or "",
        )

    monkeypatch.setattr(_claude_cli, "run", fake_run)
    monkeypatch.setattr(_claude_cli, "ensure_available", lambda: "/fake/claude")

    result = skill_validation.validate_skill("demo-skill", skills_root=tmp_path)

    assert result["ok"] is True
    assert result["skillPath"] == str(skill_dir / "SKILL.md")


def test_validate_skill_handles_skill_with_no_success_criteria(monkeypatch, tmp_path: Path) -> None:
    """A skill with no ## Success criteria block returns ok=False, criteria=[], no claude call."""
    text = (
        "---\nname: empty-skill\ndescription: Has no criteria.\n---\n\n"
        "# empty-skill\n## How to apply\nDo nothing.\n"
    )
    skill_dir = tmp_path / ".claude" / "skills" / "empty-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fail_if_called(*a, **kw):  # noqa: ANN001
        raise AssertionError("claude -p should NOT be invoked for a skill with no criteria")

    monkeypatch.setattr(_claude_cli, "run", fail_if_called)
    monkeypatch.setattr(_claude_cli, "ensure_available", lambda: "/fake/claude")

    result = skill_validation.validate_skill("empty-skill")
    assert result["ok"] is False
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["criteria"] == []
    assert "no ## Success criteria" in result["error"]


def test_validate_skill_cleans_up_sandbox_unless_kept(monkeypatch, tmp_path: Path) -> None:
    skill_dir = tmp_path / ".claude" / "skills" / "echo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SAMPLE_SKILL, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    captured_sandboxes: list[str] = []

    def capturing_run(prompt: str, *, cwd: str | None = None, timeout_seconds: float = 300.0,
                     extra_args=None):  # noqa: ANN001
        captured_sandboxes.append(cwd or "")
        return _claude_cli.ClaudeCliResult(
            stdout='```json\n{"criteria": [{"name": "x", "passed": true, "evidence": "ok"},'
                   '{"name": "y", "passed": true, "evidence": "ok"}]}\n```\n',
            stderr="",
            exit_code=0,
            wall_seconds=0.1,
            cwd=cwd or "",
        )

    monkeypatch.setattr(_claude_cli, "run", capturing_run)
    monkeypatch.setattr(_claude_cli, "ensure_available", lambda: "/fake/claude")

    # Default: cleanup
    result = skill_validation.validate_skill("echo-skill")
    assert result["sandboxDir"] is None
    sandbox_seen = captured_sandboxes[-1]
    assert sandbox_seen and not Path(sandbox_seen).exists()

    # With keep_sandbox: dir persists
    result_kept = skill_validation.validate_skill("echo-skill", keep_sandbox=True)
    assert result_kept["sandboxDir"] is not None
    assert Path(result_kept["sandboxDir"]).exists()
    # Cleanup so we don't leak fixtures
    import shutil
    shutil.rmtree(result_kept["sandboxDir"], ignore_errors=True)
