"""Built-in deterministic capability certification validators."""
from __future__ import annotations

from typing import Any

from .base import ValidatorContext, validator_result


class SourceTextPresentValidator:
    id = "source_text_present"
    severity = "critical"
    category = "source_fidelity"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        sections = context.capability.get("sourceBinding", {}).get("sectionBindings", [])
        source_words = sum(int(item.get("wordCount") or 0) for item in sections)
        return validator_result(
            self.id,
            passed=source_words > 0,
            check="Captured source contains lesson text.",
            evidence=f"captured source word count={source_words}",
            severity=self.severity,
            category=self.category,
            score=1.0 if source_words > 0 else 0.0,
        )


class EvidenceBindingsPresentValidator:
    id = "evidence_bindings_present"
    severity = "critical"
    category = "source_fidelity"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        sections = context.capability.get("sourceBinding", {}).get("sectionBindings", [])
        evidence_nodes = context.evidence.get("nodes", [])
        passed = len(evidence_nodes) >= len(sections) and len(sections) > 0
        score = (len(evidence_nodes) / len(sections)) if sections else 0.0
        return validator_result(
            self.id,
            passed=passed,
            check="Each distilled section has an evidence node.",
            evidence=f"evidence_nodes={len(evidence_nodes)} sections={len(sections)}",
            severity=self.severity,
            category=self.category,
            score=score,
        )


class CodifiedAssetPresentValidator:
    id = "codified_asset_present"
    severity = "critical"
    category = "codification"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        passed = bool(context.codify_result and context.codify_result.get("ok"))
        return validator_result(
            self.id,
            passed=passed,
            check="Generated asset has been codified.",
            evidence=f"provider={(context.codify_result or {}).get('provider')}",
            severity=self.severity,
            category=self.category,
            score=1.0 if passed else 0.0,
        )


class ExecutionValidationPassedValidator:
    id = "execution_validation_passed"
    category = "execution_validation"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        validation_ok = bool(context.validation_result and context.validation_result.get("ok"))
        validation_skipped = bool(
            context.validation_result is None
            or context.validation_result.get("skipped")
        )
        passed = validation_ok
        total = 0
        passed_count = 0
        if context.validation_result:
            passed_count = int(context.validation_result.get("passed") or 0)
            failed_count = int(context.validation_result.get("failed") or 0)
            total = passed_count + failed_count
        score = passed_count / total if total else 1.0 if validation_ok else 0.0
        return validator_result(
            self.id,
            passed=passed,
            check="Skill execution validation passed.",
            evidence=(
                "validation passed"
                if validation_ok
                else "validation skipped" if validation_skipped
                else "validation failed"
            ),
            severity="critical",
            category=self.category,
            skipped=validation_skipped,
            score=score,
        )


class ExportRecordedValidator:
    id = "export_recorded"
    severity = "high"
    category = "governance"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        passed = bool(context.export_result and context.export_result.get("ok"))
        return validator_result(
            self.id,
            passed=passed,
            check="Target export completed and is recorded.",
            evidence=f"target={(context.export_result or {}).get('target')}",
            severity=self.severity,
            category=self.category,
            score=1.0 if passed else 0.0,
        )


class CaptureDistillRecordedValidator:
    id = "capture_distill_recorded"
    severity = "high"
    category = "governance"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        passed = bool(context.capture_result and context.distill_result)
        return validator_result(
            self.id,
            passed=passed,
            check="Capture and distillation phases returned structured records.",
            evidence=(
                f"capture_ok={(context.capture_result or {}).get('ok')} "
                f"distill_ok={(context.distill_result or {}).get('ok')}"
            ),
            severity=self.severity,
            category=self.category,
            score=1.0 if passed else 0.0,
        )


class CapabilityContractPresentValidator:
    id = "capability_contract_present"
    severity = "high"
    category = "capability_ir"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        contract = context.capability.get("contract") or {}
        passed = bool(contract.get("inputs") and contract.get("outputs"))
        return validator_result(
            self.id,
            passed=passed,
            check="Capability IR declares inputs and outputs.",
            evidence=f"inputs={bool(contract.get('inputs'))} outputs={bool(contract.get('outputs'))}",
            severity=self.severity,
            category=self.category,
            score=1.0 if passed else 0.0,
        )


class VisualActionsPresentValidator:
    id = "visual_actions_present"
    severity = "high"
    category = "visual_grounding"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        source_kind = context.capability.get("sourceKind")
        video_source = source_kind in {"youtube_video", "local_video"}
        if not video_source:
            return validator_result(
                self.id,
                passed=True,
                skipped=True,
                check="Structured visual actions are required for video sources.",
                evidence=f"sourceKind={source_kind}",
                severity=self.severity,
                category=self.category,
                score=1.0,
            )
        nodes = context.evidence.get("nodes") or []
        action_nodes = [
            node for node in nodes
            if node.get("visualActions")
            or (node.get("visualEvidence") or {}).get("visualActionCount")
        ]
        score = len(action_nodes) / len(nodes) if nodes else 0.0
        return validator_result(
            self.id,
            passed=bool(action_nodes),
            check="Video sections include structured visual-action evidence.",
            evidence=f"visual_action_sections={len(action_nodes)} sections={len(nodes)}",
            severity=self.severity,
            category=self.category,
            score=score,
        )


class RightsAssessmentPresentValidator:
    id = "rights_assessment_present"
    severity = "critical"
    category = "governance_rights"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        assessment = context.rights_assessment or context.capability.get("rights") or {}
        basis = str(assessment.get("rightsBasis") or "unknown")
        export_allowed = assessment.get("exportAllowed")
        passed = bool(assessment) and basis != "unknown" and export_allowed is not False
        score = 1.0 if passed else 0.4 if assessment and export_allowed in (True, "private_only") else 0.0
        return validator_result(
            self.id,
            passed=passed,
            check="Capability has a usable rights and provenance assessment.",
            evidence=(
                f"rightsBasis={basis} exportAllowed={export_allowed} "
                f"risk={assessment.get('fairUseRisk')}"
            ),
            severity=self.severity,
            category=self.category,
            score=score,
        )


class SourceSecurityAssessmentValidator:
    id = "source_prompt_injection_guard_passed"
    severity = "critical"
    category = "governance_security"

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        assessment = (
            context.source_security_assessment
            or (context.capability.get("security") or {}).get("promptInjection")
            or {}
        )
        blocked = bool(assessment.get("blocked"))
        match_count = int(assessment.get("matchCount") or 0)
        passed = bool(assessment) and not blocked
        score = 1.0 if passed and match_count == 0 else 0.75 if passed else 0.0
        return validator_result(
            self.id,
            passed=passed,
            check="Captured source passed prompt-injection screening before capability generation.",
            evidence=(
                f"blocked={blocked} riskLevel={assessment.get('riskLevel')} "
                f"matches={match_count}"
            ),
            severity=self.severity,
            category=self.category,
            score=score,
        )


def builtin_validators() -> list[Any]:
    return [
        SourceTextPresentValidator(),
        EvidenceBindingsPresentValidator(),
        CodifiedAssetPresentValidator(),
        ExecutionValidationPassedValidator(),
        ExportRecordedValidator(),
        CaptureDistillRecordedValidator(),
        CapabilityContractPresentValidator(),
        VisualActionsPresentValidator(),
        RightsAssessmentPresentValidator(),
        SourceSecurityAssessmentValidator(),
    ]
