"""Deterministic source prompt-injection scanning for SkillMint."""
from __future__ import annotations

import re
from typing import Any, Iterable


PROMPT_INJECTION_SCHEMA = "skillmint.prompt_injection_assessment.v1"


class PromptInjectionPolicyError(RuntimeError):
    """Raised when source content tries to control the capability generator."""


_SOURCE_LIMIT_CHARS = 400_000
_SNIPPET_RADIUS = 96


_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "id": "instruction_override",
        "severity": "critical",
        "category": "source_security",
        "description": "Source tells the compiler or model to ignore governing instructions.",
        "pattern": re.compile(
            r"\b(?:ignore|disregard|override|bypass|forget)\b.{0,80}"
            r"\b(?:previous|prior|above|system|developer|tool|safety|policy|instruction|rules?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
    {
        "id": "forbid_system_compliance",
        "severity": "critical",
        "category": "source_security",
        "description": "Source tells the compiler or model not to follow system or developer instructions.",
        "pattern": re.compile(
            r"\b(?:do not|don't|never|stop)\b.{0,80}"
            r"\b(?:follow|obey|honou?r|respect|apply)\b.{0,80}"
            r"\b(?:system|developer|previous|prior|above|tool|safety|policy)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
    {
        "id": "actor_directed_skill_creation",
        "severity": "critical",
        "category": "source_security",
        "description": "Source directly asks SkillMint, Codex, Claude, or an agent to create/export a capability.",
        "pattern": re.compile(
            r"\b(?:skillmint|codex|claude|chatgpt|assistant|agent|model|compiler)\b"
            r"\s*[:,-]\s*(?:(?!\n\n).{0,120})?"
            r"\b(?:create|generate|write|save|export|install|mint|publish)\b.{0,80}"
            r"\b(?:skill|agent|workflow|capability|playbook|tool)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
    {
        "id": "generator_role_reassignment",
        "severity": "high",
        "category": "source_security",
        "description": "Source tries to reassign the model or compiler role.",
        "pattern": re.compile(
            r"\b(?:you are now|act as|pretend to be|from now on you are|new role:)\b"
            r".{0,120}\b(?:system|developer|assistant|agent|compiler|skillmint|codex|claude)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
    {
        "id": "secret_exfiltration",
        "severity": "critical",
        "category": "source_security",
        "description": "Source tries to make the agent read or leak secrets.",
        "pattern": re.compile(
            r"\b(?:read|steal|leak|print|dump|exfiltrate|send|upload|copy)\b.{0,100}"
            r"\b(?:secret|token|api[_ -]?key|credential|password|private[_ -]?key|env(?:ironment)? variable)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
    {
        "id": "tool_or_shell_control",
        "severity": "high",
        "category": "source_security",
        "description": "Source tries to direct tool, shell, MCP, or browser execution.",
        "pattern": re.compile(
            r"\b(?:run|execute|invoke|call|use|open)\b.{0,100}"
            r"\b(?:shell|powershell|cmd|terminal|mcp|tool|browser|webhook|http request)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
    {
        "id": "hidden_instruction_marker",
        "severity": "high",
        "category": "source_security",
        "description": "Source labels content as hidden or prompt-injection instructions.",
        "pattern": re.compile(
            r"\b(?:prompt injection|hidden instructions?|system prompt|developer message|jailbreak)\b",
            re.IGNORECASE,
        ),
    },
    {
        "id": "hostile_output_contract",
        "severity": "high",
        "category": "source_security",
        "description": "Source tries to control the compiler output instead of teaching source material.",
        "pattern": re.compile(
            r"\b(?:return|output|respond with|write)\b.{0,80}"
            r"\b(?:only|exactly|nothing else)\b.{0,80}"
            r"\b(?:markdown|yaml|json|skill|agent|workflow|frontmatter)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    },
)


def scan_source_for_prompt_injection(
    *,
    playbook_name: str,
    source_kind: str,
    manifest: dict[str, Any] | None,
    lessons: dict[str, Any] | None,
    steps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic prompt-injection assessment for captured source."""
    matches: list[dict[str, Any]] = []
    scanned_chars = 0
    segments_scanned = 0
    for location, text in _iter_source_text(manifest or {}, lessons or {}, steps or {}):
        if scanned_chars >= _SOURCE_LIMIT_CHARS:
            break
        text = str(text or "")
        if not text.strip():
            continue
        remaining = _SOURCE_LIMIT_CHARS - scanned_chars
        text = text[:remaining]
        scanned_chars += len(text)
        segments_scanned += 1
        matches.extend(_scan_segment(location, text))

    critical_count = sum(1 for match in matches if match["severity"] == "critical")
    high_count = sum(1 for match in matches if match["severity"] == "high")
    score = _risk_score(critical_count=critical_count, high_count=high_count, match_count=len(matches))
    blocked = critical_count > 0 or high_count >= 2
    risk_level = "critical" if critical_count else "high" if high_count >= 2 else "medium" if high_count else "low"
    return {
        "schema": PROMPT_INJECTION_SCHEMA,
        "ok": not blocked,
        "blocked": blocked,
        "riskLevel": risk_level,
        "riskScore": score,
        "playbookName": playbook_name,
        "sourceKind": source_kind,
        "segmentsScanned": segments_scanned,
        "charactersScanned": scanned_chars,
        "matchCount": len(matches),
        "criticalMatchCount": critical_count,
        "highMatchCount": high_count,
        "matches": matches[:25],
        "truncated": len(matches) > 25 or scanned_chars >= _SOURCE_LIMIT_CHARS,
    }


def assert_prompt_injection_safe(assessment: dict[str, Any]) -> None:
    """Raise when the source-security assessment blocks capability creation."""
    if assessment.get("blocked"):
        raise PromptInjectionPolicyError(format_prompt_injection_block(assessment))


def format_prompt_injection_block(assessment: dict[str, Any]) -> str:
    """Render a concise user-facing block message."""
    matches = assessment.get("matches") or []
    lead = (
        "prompt injection guard blocked skill creation; source content appears to "
        "target the SkillMint compiler or agent runtime"
    )
    if not matches:
        return lead
    parts = []
    for match in matches[:3]:
        parts.append(
            f"{match.get('id')} at {match.get('location')}: {match.get('snippet')}"
        )
    return f"{lead}. Matches: " + " | ".join(parts)


def _scan_segment(location: str, text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for rule in _PATTERNS:
        for found in rule["pattern"].finditer(text):
            matches.append(
                {
                    "id": rule["id"],
                    "severity": rule["severity"],
                    "category": rule["category"],
                    "description": rule["description"],
                    "location": location,
                    "start": found.start(),
                    "end": found.end(),
                    "snippet": _snippet(text, found.start(), found.end()),
                }
            )
            break
    return matches


def _iter_source_text(
    manifest: dict[str, Any],
    lessons: dict[str, Any],
    steps: dict[str, Any],
) -> Iterable[tuple[str, str]]:
    if manifest.get("summary"):
        yield "manifest.summary", str(manifest["summary"])
    video = manifest.get("video") if isinstance(manifest.get("video"), dict) else {}
    if video.get("title"):
        yield "manifest.video.title", str(video["title"])
    for section in lessons.get("sections") or []:
        if not isinstance(section, dict):
            continue
        ordinal = section.get("ordinal") or "unknown"
        for key in ("title", "heading", "text"):
            if section.get(key):
                yield f"lessons.sections[{ordinal}].{key}", str(section[key])
        for idx, action in enumerate(section.get("visualActions") or []):
            if not isinstance(action, dict):
                continue
            for key in ("visibleTextSample", "addedTextSample"):
                if action.get(key):
                    yield f"lessons.sections[{ordinal}].visualActions[{idx}].{key}", str(action[key])
    for step in steps.get("steps") or []:
        if not isinstance(step, dict):
            continue
        ordinal = step.get("ordinal") or "unknown"
        if step.get("captionText"):
            yield f"steps[{ordinal}].captionText", str(step["captionText"])
        action = step.get("visualAction") if isinstance(step.get("visualAction"), dict) else {}
        ocr = action.get("ocr") if isinstance(action.get("ocr"), dict) else {}
        for key in ("visibleTextSample", "addedTextSample"):
            if ocr.get(key):
                yield f"steps[{ordinal}].visualAction.ocr.{key}", str(ocr[key])


def _snippet(text: str, start: int, end: int) -> str:
    prefix = max(0, start - _SNIPPET_RADIUS)
    suffix = min(len(text), end + _SNIPPET_RADIUS)
    snippet = re.sub(r"\s+", " ", text[prefix:suffix]).strip()
    if prefix > 0:
        snippet = "..." + snippet
    if suffix < len(text):
        snippet += "..."
    return snippet


def _risk_score(*, critical_count: int, high_count: int, match_count: int) -> float:
    score = (critical_count * 0.55) + (high_count * 0.22) + max(0, match_count - critical_count - high_count) * 0.08
    return round(min(1.0, score), 3)
