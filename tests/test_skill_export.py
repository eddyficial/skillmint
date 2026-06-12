"""Tests for exporting Skillmint assets to other agent formats."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillmint import skill_export as se


def _write_skill(tmp_path: Path) -> Path:
    path = tmp_path / ".claude" / "skills" / "demo-skill" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Applies a demo process.\n"
        "inputs:\n"
        "  user_prompt: string\n"
        "---\n\n"
        "# demo-skill\n\n"
        "## How to apply\n\n"
        "Follow the demo process.\n",
        encoding="utf-8",
    )
    return path


def test_export_codex_copies_skill_to_agents_skill_folder(tmp_path: Path) -> None:
    source = _write_skill(tmp_path)

    result = se.export_skill_asset(
        source,
        target="codex",
        project_root=tmp_path,
        overwrite=True,
    )

    output = tmp_path / ".agents" / "skills" / "demo-skill" / "SKILL.md"
    sidecar = tmp_path / ".agents" / "skills" / "demo-skill" / "skillmint.json"
    assert result["outputPath"] == str(output)
    assert result["linkManifestPath"] == str(sidecar)
    assert output.read_text(encoding="utf-8").startswith("---\nname: demo-skill")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["schema"] == "skillmint.link.v1"
    assert payload["target"] == "codex"
    assert payload["assetPath"] == str(output)


def test_export_cursor_writes_mdc_rule(tmp_path: Path) -> None:
    source = _write_skill(tmp_path)

    result = se.export_skill_asset(
        source,
        target="cursor",
        project_root=tmp_path,
        overwrite=True,
    )

    output = Path(result["outputPath"])
    body = output.read_text(encoding="utf-8")
    assert output == tmp_path / ".cursor" / "rules" / "demo-skill.mdc"
    assert Path(result["linkManifestPath"]) == tmp_path / ".cursor" / "rules" / "demo-skill.skillmint.json"
    assert body.startswith("---\ndescription: \"Applies a demo process.\"")
    assert "alwaysApply: false" in body
    assert "Follow the demo process." in body


def test_export_windsurf_and_markdown_targets(tmp_path: Path) -> None:
    source = _write_skill(tmp_path)

    windsurf = se.export_skill_asset(
        source,
        target="windsurf",
        project_root=tmp_path,
        overwrite=True,
    )
    markdown = se.export_skill_asset(
        source,
        target="markdown",
        project_root=tmp_path,
        overwrite=True,
    )

    assert Path(windsurf["outputPath"]) == tmp_path / ".windsurf" / "rules" / "demo-skill.md"
    assert Path(markdown["outputPath"]) == tmp_path / ".skillmint" / "exports" / "markdown" / "demo-skill.md"
    assert "Target: Windsurf rule" in Path(windsurf["outputPath"]).read_text(encoding="utf-8")
    assert "Export target: portable Markdown" in Path(markdown["outputPath"]).read_text(encoding="utf-8")


def test_export_rejects_unknown_target(tmp_path: Path) -> None:
    source = _write_skill(tmp_path)

    with pytest.raises(se.SkillExportError, match="invalid target"):
        se.export_skill_asset(source, target="unknown", project_root=tmp_path)
