"""Validate a Skillmint-produced skill by actually executing it.

Reads a saved SKILL.md, parses its input contract from the ``## Inputs`` body
section (falling back to a legacy ``inputs:`` frontmatter key if present) and
its ``## Success criteria`` block, materializes deterministic sample inputs,
spawns ``claude -p`` against the skill body in a sandbox, parses the resulting
PASS/FAIL report, and returns a structured result.

Cost is bounded: one CLI invocation per validate call, no LLM-driven input
generation, no batch, no caching. If the user needs more, they call again.

This is the keystone of the skillmint feedback loop. Until this module exists,
every claim about a produced skill is trust-based. With it, a skill carries a
machine-readable pass/fail score against its declared criteria.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _claude_cli


class SkillValidationError(RuntimeError):
    """Raised when validation can't even start — missing skill, malformed SKILL.md, etc."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_skill(
    skill_name: str,
    *,
    sample_inputs: dict[str, Any] | None = None,
    keep_sandbox: bool = False,
    timeout_seconds: float = 300.0,
    skills_root: str | Path | None = None,
) -> dict[str, Any]:
    """Run a saved skill against its declared success criteria, return a report.

    Looks up ``<skills_root>/.claude/skills/<skill_name>/SKILL.md`` when a root
    is supplied, then ``<cwd>/.claude/skills/<skill_name>/SKILL.md``, then
    ``~/.claude/skills/<skill_name>/SKILL.md``. Raises SkillValidationError if
    none exists.

    ``sample_inputs`` overrides the deterministic defaults derived from the
    skill's YAML ``inputs:`` schema. Pass an empty dict to suppress defaults.

    ``keep_sandbox`` leaves the temp directory in place for inspection; the
    returned ``sandboxDir`` is the path. Otherwise the dir is removed before
    return.
    """
    skill_path = _resolve_skill_path(skill_name, skills_root=skills_root)
    text = skill_path.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(text)
    criteria = _parse_success_criteria(text)

    if not criteria:
        return {
            "ok": False,
            "skillName": skill_name,
            "skillPath": str(skill_path),
            "passed": 0,
            "failed": 0,
            "criteria": [],
            "wallSeconds": 0.0,
            "sandboxDir": None,
            "claudeExitCode": -1,
            "claudeStderr": "",
            "error": "skill has no ## Success criteria block; nothing to validate",
        }

    # Canonical source is the '## Inputs' body section; fall back to a
    # legacy/hand-authored `inputs:` frontmatter key when the body has none.
    inputs_schema = _parse_inputs_section(text) or frontmatter.get("inputs")
    inputs = sample_inputs if sample_inputs is not None else _materialize_sample_inputs(
        inputs_schema,
    )

    sandbox_dir = tempfile.mkdtemp(prefix="skillmint-validate-")
    started = time.monotonic()
    try:
        # Re-resolve any input values that referenced the sandbox placeholder.
        resolved_inputs = _resolve_sandbox_placeholders(inputs, sandbox_dir)
        prompt = _build_prompt(text, resolved_inputs, criteria, sandbox_dir)
        cli_result = _claude_cli.run(
            prompt,
            cwd=sandbox_dir,
            timeout_seconds=timeout_seconds,
        )
        report = _parse_report(cli_result.stdout, criteria)
        passed = sum(1 for c in report if c["passed"])
        failed = len(report) - passed
        wall = round(time.monotonic() - started, 2)

        return {
            "ok": failed == 0 and cli_result.exit_code == 0,
            "skillName": skill_name,
            "skillPath": str(skill_path),
            "passed": passed,
            "failed": failed,
            "criteria": report,
            "wallSeconds": wall,
            "sandboxDir": sandbox_dir if keep_sandbox else None,
            "claudeExitCode": cli_result.exit_code,
            "claudeStderr": _truncate(cli_result.stderr, 4096),
            "sampleInputs": resolved_inputs,
        }
    finally:
        if not keep_sandbox:
            shutil.rmtree(sandbox_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", flags=re.DOTALL)
_SUCCESS_HEADER_RE = re.compile(r"^##\s+Success criteria\s*$", flags=re.MULTILINE)
_INPUTS_HEADER_RE = re.compile(r"^##\s+Inputs\s*$", flags=re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^##\s+\S", flags=re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*-\s+(.*\S)\s*$", flags=re.MULTILINE)
_INPUTS_BULLET_RE = re.compile(
    r"^\s*-\s+`(?P<name>[A-Za-z_][\w]*)`\s*\((?P<spec>[^)]*)\)\s*:",
    flags=re.MULTILINE,
)


def _resolve_skill_path(skill_name: str, *, skills_root: str | Path | None = None) -> Path:
    """Resolve a skill slug to its SKILL.md, project-local first, then user-global."""
    candidates: list[Path] = []
    if skills_root is not None:
        root = Path(skills_root)
        parts = root.parts[-2:]
        if len(parts) == 2 and parts[-2] == ".claude" and parts[-1] == "skills":
            candidates.append(root / skill_name / "SKILL.md")
        else:
            candidates.append(root / ".claude" / "skills" / skill_name / "SKILL.md")
    candidates.extend(
        [
            Path.cwd() / ".claude" / "skills" / skill_name / "SKILL.md",
            Path.home() / ".claude" / "skills" / skill_name / "SKILL.md",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    searched = "\n  ".join(str(p) for p in candidates)
    raise SkillValidationError(
        f"skill {skill_name!r} not found; searched:\n  {searched}"
    )


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract the YAML frontmatter as a dict. Falls back to {} on parse failure.

    Skillmint frontmatter is shallow and predictable — we don't pull in PyYAML
    just for this. A few common shapes are handled inline; anything more
    complex is treated as raw text and the caller works without it.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    body = match.group(1)
    return _shallow_yaml(body)


def _shallow_yaml(body: str) -> dict[str, Any]:
    """Parse a minimal subset of YAML: top-level scalar keys + nested mapping/list.

    Sufficient for skillmint scaffolds. NOT a real YAML parser. If the input
    deviates from the scaffold shape, returns {} and lets validate_skill fall
    back to empty-inputs mode.
    """
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_collected: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Top-level "key: value" or "key:" (begin nested block)
        if re.match(r"^[A-Za-z_][\w-]*\s*:", line):
            if current_key is not None:
                result[current_key] = _coerce_yaml_value("\n".join(current_collected))
                current_collected = []
            key, _, after_colon = line.partition(":")
            current_key = key.strip()
            after_colon = after_colon.strip()
            if after_colon and not after_colon.startswith(("|", ">")):
                # Scalar on the same line
                result[current_key] = _coerce_scalar(after_colon)
                current_key = None
            # Else: nested block follows on subsequent indented lines
        elif current_key is not None:
            current_collected.append(line)
    if current_key is not None:
        result[current_key] = _coerce_yaml_value("\n".join(current_collected))
    return result


def _coerce_scalar(token: str) -> Any:
    token = token.strip().strip(",")
    if token.lower() in ("null", "none", "~", ""):
        return None
    if token.lower() == "true":
        return True
    if token.lower() == "false":
        return False
    if (token.startswith('"') and token.endswith('"')) or (token.startswith("'") and token.endswith("'")):
        return token[1:-1]
    try:
        return int(token)
    except ValueError:
        try:
            return float(token)
        except ValueError:
            return token


def _coerce_yaml_value(block: str) -> Any:
    """Coerce a block of nested YAML lines into dict / list / scalar / raw."""
    stripped = block.strip()
    if not stripped:
        return None
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if all(re.match(r"^\s*-\s+", ln) for ln in lines):
        return [_coerce_scalar(re.sub(r"^\s*-\s+", "", ln)) for ln in lines]
    nested: dict[str, Any] = {}
    for ln in lines:
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$", ln)
        if m:
            nested[m.group(1).strip()] = _coerce_scalar(m.group(2))
    if nested:
        return nested
    return stripped


def _parse_inputs_section(text: str) -> dict[str, str]:
    """Extract a ``{name: "type, required|optional"}`` schema from the '## Inputs' body section.

    This is the canonical source for a skill's input contract. SkillMint no
    longer emits `inputs:` in YAML frontmatter — generated SKILL.md files
    now carry only `name`/`description` there, matching the official Agent
    Skills spec — so validate_skill reads the same information back out of
    the body instead. Bullets look like `` - `name` (type, required):
    description `` ; only the parenthetical is captured and handed to
    ``_materialize_sample_inputs``. The free-text description after the
    colon is deliberately ignored, so a phrase like "local paths" in a
    description can't be mistaken for a path-typed argument.
    """
    header_match = _INPUTS_HEADER_RE.search(text)
    if not header_match:
        return {}
    body_start = header_match.end()
    after = text[body_start:]
    next_header = _NEXT_H2_RE.search(after)
    body = after if not next_header else after[: next_header.start()]
    return {
        m.group("name"): m.group("spec").strip()
        for m in _INPUTS_BULLET_RE.finditer(body)
    }


def _parse_success_criteria(text: str) -> list[str]:
    """Return the bullet lines under the ## Success criteria header.

    Stops at the next H2. Each bullet's text (without the leading '- ') is
    returned in document order. Returns [] if the section is missing.
    """
    header_match = _SUCCESS_HEADER_RE.search(text)
    if not header_match:
        return []
    body_start = header_match.end()
    after = text[body_start:]
    next_header = _NEXT_H2_RE.search(after)
    body = after if not next_header else after[: next_header.start()]
    return [m.group(1).strip() for m in _BULLET_RE.finditer(body)]


# ---------------------------------------------------------------------------
# Sample input materialization
# ---------------------------------------------------------------------------


_TYPE_RE = re.compile(
    r"^\s*"
    r"(?P<core>[A-Za-z_][\w.\[\], '\"|]*?)"
    r"\s*(?:\(.*?\))?"
    r"\s*(?:—|--|-)?\s*"
    r"(?P<rest>.*)?$"
)


def _materialize_sample_inputs(inputs_schema: Any) -> dict[str, Any]:
    """Generate deterministic sample inputs from a parsed YAML inputs: schema.

    The schema looks like ``{arg_name: "TypeDescription (required) — note"}``.
    We don't try to be clever — just enough to let a skill execute its happy
    path. The sandbox-placeholder string ``<SANDBOX>`` is used for any
    path-typed input; resolved against the actual sandbox dir at call time.
    """
    if not isinstance(inputs_schema, dict):
        return {}
    out: dict[str, Any] = {}
    for name, descr in inputs_schema.items():
        out[name] = _default_for(name, str(descr or ""))
    return out


def _default_for(name: str, descr: str) -> Any:
    """Pick a deterministic default value based on the type-description string."""
    low = descr.lower()
    if "pathlib.path" in low or low.startswith("path") or "filepath" in low or "directory" in low:
        return f"<SANDBOX>/{name}"
    if "literal[" in low:
        # Extract first listed option
        m = re.search(r"literal\[(.+?)\]", descr, flags=re.IGNORECASE)
        if m:
            first = m.group(1).split(",")[0].strip().strip("'\"")
            return first
        return "default"
    if "bool" in low:
        return True
    if low.startswith("int") or " int " in f" {low} ":
        return 1
    if "float" in low:
        return 1.0
    if "list[" in low:
        return []
    if "dict[" in low or low.startswith("dict"):
        return {}
    # Default: a stringy placeholder
    return f"<sample-{name}>"


def _resolve_sandbox_placeholders(inputs: dict[str, Any], sandbox_dir: str) -> dict[str, Any]:
    """Replace any '<SANDBOX>/X' string values with absolute paths under sandbox_dir."""
    out: dict[str, Any] = {}
    for key, value in inputs.items():
        if isinstance(value, str) and value.startswith("<SANDBOX>"):
            tail = value[len("<SANDBOX>"):].lstrip("/\\")
            out[key] = os.path.join(sandbox_dir, tail) if tail else sandbox_dir
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Prompt + report
# ---------------------------------------------------------------------------


def _build_prompt(
    skill_body: str,
    inputs: dict[str, Any],
    criteria: list[str],
    sandbox_dir: str,
) -> str:
    """Compose the prompt sent to `claude -p` for one validation run."""
    inputs_json = json.dumps(inputs, indent=2)
    numbered_criteria = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(criteria))
    return (
        "You are executing a Skillmint skill against its declared Success criteria.\n"
        f"Working directory: {sandbox_dir}\n"
        "Use ONLY this directory for any file writes or reads. Do not touch paths outside it.\n\n"
        f"Sample inputs to use (JSON):\n```json\n{inputs_json}\n```\n\n"
        "Execute the skill below by following its 'How to apply' section using the\n"
        "sample inputs above. Then, for EACH criterion listed under 'Success criteria',\n"
        "report PASS or FAIL with one-line evidence.\n\n"
        "Return your report as JSON in a fenced ```json block at the end of your\n"
        "response, with this exact schema:\n"
        '{"criteria": [{"name": "<one-line criterion summary>", "passed": true|false, "evidence": "..."}]}\n\n'
        "There must be one entry per criterion in the order listed:\n"
        f"{numbered_criteria}\n\n"
        "===== SKILL BODY BELOW =====\n"
        f"{skill_body}\n"
        "===== END SKILL BODY =====\n"
    )


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(\{.*?\})\s*\n```", flags=re.DOTALL)
_PASS_FAIL_LINE_RE = re.compile(
    r"^\s*(?P<num>\d+)\.\s*(?P<verdict>PASS|FAIL)\b[:\-\s]*(?P<rest>.*)$",
    flags=re.MULTILINE | re.IGNORECASE,
)


def _parse_report(stdout: str, criteria: list[str]) -> list[dict[str, Any]]:
    """Pull the criteria report out of `claude -p`'s stdout.

    Preferred path: the fenced ```json block. Fallback: regex over PASS/FAIL
    lines that reference each criterion. Anything claude didn't report is
    marked failed with evidence="not reported by executor."
    """
    by_index: dict[int, dict[str, Any]] = {}

    block_match = _JSON_BLOCK_RE.search(stdout)
    if block_match:
        try:
            parsed = json.loads(block_match.group(1))
            for i, item in enumerate(parsed.get("criteria", []) or []):
                if not isinstance(item, dict):
                    continue
                by_index[i] = {
                    "name": str(item.get("name") or _short_name(criteria[i]) if i < len(criteria) else f"criterion {i + 1}"),
                    "check": criteria[i] if i < len(criteria) else "",
                    "passed": bool(item.get("passed")),
                    "evidence": (item.get("evidence") or None) if item.get("evidence") is not None else None,
                }
        except json.JSONDecodeError:
            pass

    # Regex fallback for any indices the JSON didn't cover
    if len(by_index) < len(criteria):
        for m in _PASS_FAIL_LINE_RE.finditer(stdout):
            idx = int(m.group("num")) - 1
            if idx not in by_index and 0 <= idx < len(criteria):
                verdict = m.group("verdict").upper() == "PASS"
                by_index[idx] = {
                    "name": _short_name(criteria[idx]),
                    "check": criteria[idx],
                    "passed": verdict,
                    "evidence": m.group("rest").strip() or None,
                }

    report: list[dict[str, Any]] = []
    for i, criterion in enumerate(criteria):
        if i in by_index:
            entry = by_index[i]
            # Always overlay the canonical check text so callers can trust it
            entry["check"] = criterion
            entry.setdefault("name", _short_name(criterion))
            report.append(entry)
        else:
            report.append(
                {
                    "name": _short_name(criterion),
                    "check": criterion,
                    "passed": False,
                    "evidence": "not reported by executor",
                }
            )
    return report


def _short_name(criterion: str) -> str:
    """Take the first 80 chars of a criterion line for a compact display name."""
    cleaned = re.sub(r"\s+", " ", criterion).strip()
    return cleaned[:80] + ("…" if len(cleaned) > 80 else "")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
