"""Export Skillmint-produced assets into other agent/tool conventions."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .tutorial_playbooks import _slugify


class SkillExportError(RuntimeError):
    """Raised when an export target cannot be written."""


TARGET_ALIASES = {
    "claude": "claude_code",
    "claude_code": "claude_code",
    "claude-code": "claude_code",
    "codex": "codex",
    "openai_codex": "codex",
    "cursor": "cursor",
    "windsurf": "windsurf",
    "markdown": "markdown",
    "md": "markdown",
    "portable_markdown": "markdown",
}

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?\n)---\s*\n", flags=re.DOTALL)


def export_skill_asset(
    source_path: str | Path,
    *,
    target: str = "claude_code",
    skill_name: str | None = None,
    project_root: str | Path | None = None,
    overwrite: bool = False,
    shape: str = "skill",
    playbook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export a generated Claude-style asset to a target-specific file."""
    resolved_target = resolve_export_target(target)
    source = Path(source_path)
    if not source.is_file():
        raise SkillExportError(f"source skill asset not found: {source}")

    text = source.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(text)
    resolved_name = (skill_name or metadata.get("name") or source.stem).strip()
    if not resolved_name:
        raise SkillExportError("skill_name is required for export")
    slug = _slugify(resolved_name)
    root = _normalize_project_root(project_root, source)

    if resolved_target == "claude_code":
        sidecar_path = write_skillmint_link_manifest(
            source,
            target=resolved_target,
            skill_name=resolved_name,
            shape=shape,
            playbook=playbook,
        )
        return {
            "ok": True,
            "target": resolved_target,
            "outputPath": str(source),
            "outputDirectory": str(source.parent),
            "bytesWritten": source.stat().st_size,
            "native": True,
            "linkManifestPath": str(sidecar_path),
        }

    output_path = _target_path(
        root,
        target=resolved_target,
        slug=slug,
        shape=shape,
    )
    if output_path.exists() and not overwrite:
        raise SkillExportError(
            f"export target already exists at {output_path}; pass overwrite=true to replace it"
        )

    rendered = _render_target(
        target=resolved_target,
        name=resolved_name,
        slug=slug,
        description=str(metadata.get("description") or f"Skillmint export for {resolved_name}."),
        original_text=text,
        body=body,
        shape=shape,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    sidecar_path = write_skillmint_link_manifest(
        output_path,
        target=resolved_target,
        skill_name=resolved_name,
        shape=shape,
        playbook=playbook,
        source_path=source,
    )
    return {
        "ok": True,
        "target": resolved_target,
        "outputPath": str(output_path),
        "outputDirectory": str(output_path.parent),
        "bytesWritten": len(rendered.encode("utf-8")),
        "native": False,
        "sourcePath": str(source),
        "linkManifestPath": str(sidecar_path),
    }


def write_skillmint_link_manifest(
    asset_path: str | Path,
    *,
    target: str,
    skill_name: str,
    shape: str,
    playbook: dict[str, Any] | None,
    source_path: str | Path | None = None,
) -> Path:
    """Write machine-readable Skillmint provenance next to an exported asset."""
    asset = Path(asset_path)
    if asset.name == "SKILL.md":
        sidecar = asset.parent / "skillmint.json"
    else:
        sidecar = asset.with_name(f"{asset.stem}.skillmint.json")
    payload = {
        "schema": "skillmint.link.v1",
        "skillName": skill_name,
        "shape": shape,
        "target": target,
        "assetPath": str(asset),
        "sourceAssetPath": str(source_path) if source_path else None,
        "playbook": playbook,
    }
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return sidecar


def resolve_export_target(target: str) -> str:
    key = (target or "claude_code").strip().lower().replace(" ", "_")
    if key not in TARGET_ALIASES:
        expected = ", ".join(sorted(TARGET_ALIASES))
        raise SkillExportError(f"invalid target={target!r}; expected one of: {expected}")
    return TARGET_ALIASES[key]


def _normalize_project_root(project_root: str | Path | None, source_path: Path) -> Path:
    if project_root is None:
        return Path.cwd()
    root = Path(project_root)
    parts = root.parts[-2:]
    if len(parts) == 2 and parts[-2] == ".claude" and parts[-1] in (
        "skills",
        "agents",
        "workflows",
    ):
        return root.parent.parent
    return root


def _target_path(root: Path, *, target: str, slug: str, shape: str) -> Path:
    if target == "codex":
        return root / ".agents" / "skills" / slug / "SKILL.md"
    if target == "cursor":
        return root / ".cursor" / "rules" / f"{slug}.mdc"
    if target == "windsurf":
        return root / ".windsurf" / "rules" / f"{slug}.md"
    if target == "markdown":
        suffix = "md" if shape == "skill" else f"{shape}.md"
        return root / ".skillmint" / "exports" / "markdown" / f"{slug}.{suffix}"
    raise SkillExportError(f"unsupported target: {target}")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip() + "\n"
    raw = match.group("body")
    metadata: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[match.end():].strip() + "\n"


def _render_target(
    *,
    target: str,
    name: str,
    slug: str,
    description: str,
    original_text: str,
    body: str,
    shape: str,
) -> str:
    if target == "codex":
        return original_text.rstrip() + "\n"
    if target == "cursor":
        return (
            "---\n"
            f"description: {_yaml_string(description)}\n"
            "globs:\n"
            "  - \"**/*\"\n"
            "alwaysApply: false\n"
            "---\n\n"
            f"{body.rstrip()}\n"
        )
    if target == "windsurf":
        return (
            f"# {name}\n\n"
            f"Target: Windsurf rule\n\n"
            f"{description}\n\n"
            "## Instructions\n\n"
            f"{body.rstrip()}\n"
        )
    if target == "markdown":
        return (
            f"# {name}\n\n"
            f"Export target: portable Markdown\n\n"
            f"Source asset shape: {shape}\n"
            f"Skill slug: `{slug}`\n\n"
            f"{body.rstrip()}\n"
        )
    raise SkillExportError(f"unsupported target: {target}")


def _yaml_string(value: str) -> str:
    return json.dumps(value)
