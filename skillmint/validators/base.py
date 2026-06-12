"""Base types for SkillMint capability certification validators."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ValidatorContext:
    capability: dict[str, Any]
    evidence: dict[str, Any]
    codify_result: dict[str, Any] | None
    validation_result: dict[str, Any] | None
    capture_result: dict[str, Any]
    distill_result: dict[str, Any]
    export_result: dict[str, Any]
    rights_assessment: dict[str, Any] | None = None
    source_security_assessment: dict[str, Any] | None = None


class CapabilityValidator(Protocol):
    id: str
    severity: str
    category: str

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        """Return one structured validator result."""


def validator_result(
    validator_id: str,
    *,
    passed: bool,
    check: str,
    evidence: str,
    severity: str,
    category: str,
    skipped: bool = False,
    score: float | None = None,
) -> dict[str, Any]:
    """Normalize validator output for certification reports."""
    result = {
        "id": validator_id,
        "passed": bool(passed),
        "skipped": bool(skipped),
        "check": check,
        "evidence": evidence,
        "severity": severity,
        "category": category,
    }
    if score is not None:
        result["score"] = max(0.0, min(1.0, float(score)))
    return result
