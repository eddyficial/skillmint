"""Capability IR, evidence graph, certification, audit, and registry support.

This module is intentionally independent from any one export target. Skillmint
can keep rendering Markdown skills, Cursor rules, or other files, while the
capability package records the trust substrate teams need: source
evidence, a typed intermediate representation, validator results, certification
scores, immutable-ish audit entries, and a local registry index.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tutorial_playbooks import _playbook_dir, _slugify
from .validators import ValidatorContext, builtin_validators, domain_validators


CERTIFICATION_SCHEMA = "skillmint.certification.v1"
CAPABILITY_SCHEMA = "skillmint.capability.v1"
EVIDENCE_SCHEMA = "skillmint.evidence_graph.v1"
REGISTRY_SCHEMA = "skillmint.registry.v1"
AUDIT_SCHEMA = "skillmint.audit_event.v1"


def build_capability_package(
    *,
    skill_name: str,
    shape: str,
    source_kind: str,
    playbook_name: str,
    asset_path: str | Path,
    target_output_path: str | Path,
    codify_result: dict[str, Any] | None,
    validation_result: dict[str, Any] | None,
    capture_result: dict[str, Any],
    distill_result: dict[str, Any],
    export_result: dict[str, Any],
    skills_root: str | Path | None = None,
    require_certification: bool = False,
    rights_assessment: dict[str, Any] | None = None,
    source_security_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and persist the enterprise trust package for one generated asset."""
    playbook_dir = _playbook_dir(playbook_name)
    manifest = _read_json(playbook_dir / "manifest.json", default={})
    lessons = _read_json(playbook_dir / "lessons.json", default={})
    steps = _read_json(playbook_dir / "steps.json", default={"steps": []})
    sections = list(lessons.get("sections") or [])
    raw_steps = list(steps.get("steps") or [])

    evidence_graph = _build_evidence_graph(
        playbook_name=playbook_name,
        source_kind=source_kind,
        manifest=manifest,
        sections=sections,
        steps=raw_steps,
    )
    package_dir = _capability_package_dir(
        skills_root=skills_root,
        skill_name=skill_name,
    )
    package_dir.mkdir(parents=True, exist_ok=True)
    retained_artifacts = _retain_evidence_artifacts(
        playbook_dir=playbook_dir,
        package_dir=package_dir,
        evidence_graph=evidence_graph,
    )
    if retained_artifacts["files"]:
        evidence_graph["retainedArtifacts"] = retained_artifacts
    capability_ir = _build_capability_ir(
        skill_name=skill_name,
        shape=shape,
        source_kind=source_kind,
        playbook_name=playbook_name,
        manifest=manifest,
        sections=sections,
        evidence_graph=evidence_graph,
        asset_path=asset_path,
        target_output_path=target_output_path,
        rights_assessment=rights_assessment,
        source_security_assessment=source_security_assessment,
    )
    validators = run_certification_validators(
        capability=capability_ir,
        evidence=evidence_graph,
        codify_result=codify_result,
        validation_result=validation_result,
        capture_result=capture_result,
        distill_result=distill_result,
        export_result=export_result,
        rights_assessment=rights_assessment,
        source_security_assessment=source_security_assessment,
    )
    certification = _score_certification(
        skill_name=skill_name,
        shape=shape,
        source_kind=source_kind,
        capability=capability_ir,
        evidence=evidence_graph,
        validators=validators,
        codify_result=codify_result,
        validation_result=validation_result,
        require_certification=require_certification,
    )

    capability_path = package_dir / "capability.json"
    evidence_path = package_dir / "evidence.json"
    certification_path = package_dir / "certification.json"
    capability_path.write_text(json.dumps(capability_ir, indent=2), encoding="utf-8")
    evidence_path.write_text(json.dumps(evidence_graph, indent=2), encoding="utf-8")
    certification_path.write_text(json.dumps(certification, indent=2), encoding="utf-8")
    artifact_hashes = _artifact_hashes(
        capability_path=capability_path,
        evidence_path=evidence_path,
        certification_path=certification_path,
        asset_path=Path(asset_path),
        target_output_path=Path(target_output_path),
    )
    certification["artifactHashes"] = artifact_hashes
    certification["signature"] = _signature_for_certification(certification)
    certification_path.write_text(json.dumps(certification, indent=2), encoding="utf-8")

    audit_event = append_audit_event(
        skills_root=skills_root,
        event_type="capability.generated",
        skill_name=skill_name,
        payload={
            "shape": shape,
            "sourceKind": source_kind,
            "certificationStatus": certification["status"],
            "confidenceScore": certification["confidenceScore"],
            "promotionState": certification["promotionState"],
            "signature": certification["signature"],
            "packageDirectory": str(package_dir),
            "assetPath": str(asset_path),
            "targetOutputPath": str(target_output_path),
            "rights": {
                "rightsBasis": (rights_assessment or {}).get("rightsBasis"),
                "exportAllowed": (rights_assessment or {}).get("exportAllowed"),
                "fairUseRisk": (rights_assessment or {}).get("fairUseRisk"),
                "reviewRequired": (rights_assessment or {}).get("reviewRequired"),
            },
            "sourceSecurity": {
                "blocked": (source_security_assessment or {}).get("blocked"),
                "riskLevel": (source_security_assessment or {}).get("riskLevel"),
                "matchCount": (source_security_assessment or {}).get("matchCount"),
            },
        },
    )
    registry_entry = update_capability_registry(
        skills_root=skills_root,
        skill_name=skill_name,
        shape=shape,
        source_kind=source_kind,
        package_dir=package_dir,
        capability_path=capability_path,
        evidence_path=evidence_path,
        certification_path=certification_path,
        asset_path=Path(asset_path),
        target_output_path=Path(target_output_path),
        certification=certification,
        rights_assessment=rights_assessment,
        source_security_assessment=source_security_assessment,
    )

    return {
        "ok": certification["status"] == "certified",
        "packageDirectory": str(package_dir),
        "capabilityPath": str(capability_path),
        "evidencePath": str(evidence_path),
        "certificationPath": str(certification_path),
        "auditEvent": audit_event,
        "registryEntry": registry_entry,
        "capability": capability_ir,
        "evidence": evidence_graph,
        "certification": certification,
    }


def run_certification_validators(
    *,
    capability: dict[str, Any],
    evidence: dict[str, Any],
    codify_result: dict[str, Any] | None,
    validation_result: dict[str, Any] | None,
    capture_result: dict[str, Any],
    distill_result: dict[str, Any],
    export_result: dict[str, Any],
    rights_assessment: dict[str, Any] | None = None,
    source_security_assessment: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run built-in and domain validators over the capability package."""
    context = ValidatorContext(
        capability=capability,
        evidence=evidence,
        codify_result=codify_result,
        validation_result=validation_result,
        capture_result=capture_result,
        distill_result=distill_result,
        export_result=export_result,
        rights_assessment=rights_assessment,
        source_security_assessment=source_security_assessment,
    )
    results: list[dict[str, Any]] = []
    for validator in [*builtin_validators(), *domain_validators()]:
        results.append(validator.validate(context))
    return results


def append_audit_event(
    *,
    skills_root: str | Path | None,
    event_type: str,
    skill_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    root = _project_root(skills_root)
    audit_dir = root / ".skillmint" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    ledger = audit_dir / "capability-ledger.jsonl"
    previous_hash = _last_ledger_hash(ledger)
    event = {
        "schema": AUDIT_SCHEMA,
        "eventType": event_type,
        "skillName": skill_name,
        "createdAt": _now(),
        "previousEventHash": previous_hash,
        "payload": payload,
    }
    event_hash = _sha256_text(json.dumps(event, sort_keys=True, default=str))
    event["eventId"] = event_hash[:16]
    event["eventHash"] = event_hash
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    event["ledgerPath"] = str(ledger)
    return event


def update_capability_registry(
    *,
    skills_root: str | Path | None,
    skill_name: str,
    shape: str,
    source_kind: str,
    package_dir: Path,
    capability_path: Path,
    evidence_path: Path,
    certification_path: Path,
    asset_path: Path,
    target_output_path: Path,
    certification: dict[str, Any],
    rights_assessment: dict[str, Any] | None = None,
    source_security_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _project_root(skills_root)
    registry_dir = root / ".skillmint" / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_path = registry_dir / "capabilities.json"
    registry = _read_json(registry_path, default={"schema": REGISTRY_SCHEMA, "capabilities": {}})
    slug = _slugify(skill_name)
    entry = {
        "skillName": skill_name,
        "slug": slug,
        "shape": shape,
        "sourceKind": source_kind,
        "status": certification["status"],
        "promotionState": certification["promotionState"],
        "confidenceScore": certification["confidenceScore"],
        "signature": certification.get("signature"),
        "rightsBasis": (rights_assessment or {}).get("rightsBasis"),
        "exportAllowed": (rights_assessment or {}).get("exportAllowed"),
        "fairUseRisk": (rights_assessment or {}).get("fairUseRisk"),
        "rightsReviewRequired": (rights_assessment or {}).get("reviewRequired"),
        "sourceSecurityRisk": (source_security_assessment or {}).get("riskLevel"),
        "sourceSecurityBlocked": (source_security_assessment or {}).get("blocked"),
        "updatedAt": _now(),
        "packageDirectory": str(package_dir),
        "capabilityPath": str(capability_path),
        "evidencePath": str(evidence_path),
        "certificationPath": str(certification_path),
        "assetPath": str(asset_path),
        "targetOutputPath": str(target_output_path),
    }
    registry["capabilities"][slug] = entry
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    entry["registryPath"] = str(registry_path)
    return entry


def _build_evidence_graph(
    *,
    playbook_name: str,
    source_kind: str,
    manifest: dict[str, Any],
    sections: list[dict[str, Any]],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes = []
    edges = []
    source_url = manifest.get("sourceUrl") or manifest.get("seedUrl")
    step_by_ordinal = {step.get("ordinal"): step for step in steps}
    for section in sections:
        ordinal = section.get("ordinal") or len(nodes) + 1
        evidence_id = f"evidence:{playbook_name}:{ordinal}"
        step_refs = list(section.get("stepOrdinals") or [])
        step_evidence = [step_by_ordinal.get(ref) for ref in step_refs if ref in step_by_ordinal]
        node = {
            "id": evidence_id,
            "type": "source_section",
            "sourceKind": source_kind,
            "sourceUrl": source_url,
            "sectionOrdinal": ordinal,
            "title": section.get("title") or section.get("heading"),
            "wordCount": int(section.get("wordCount") or len(str(section.get("text") or "").split())),
            "textHash": _sha256_text(str(section.get("text") or section.get("captionText") or "")),
            "videoStartSeconds": section.get("videoStartSeconds"),
            "videoEndSeconds": section.get("videoEndSeconds"),
            "anchorKeyframePath": section.get("anchorKeyframePath"),
            "pageNumber": section.get("pageNumber") or section.get("page"),
            "stepOrdinals": step_refs,
            "visualEvidence": _visual_evidence_for_section(section, step_evidence),
            "visualActions": _visual_actions_for_section(section, step_evidence),
        }
        if step_evidence:
            node["stepHashes"] = [
                _sha256_text(json.dumps(step, sort_keys=True, default=str))
                for step in step_evidence
                if step is not None
            ]
        nodes.append(node)
        edges.append(
            {
                "from": evidence_id,
                "to": f"capability:{_slugify(playbook_name)}",
                "relationship": "supports",
            }
        )
    return {
        "schema": EVIDENCE_SCHEMA,
        "playbookName": playbook_name,
        "sourceKind": source_kind,
        "sourceUrl": source_url,
        "createdAt": _now(),
        "nodes": nodes,
        "edges": edges,
    }


def _retain_evidence_artifacts(
    *,
    playbook_dir: Path,
    package_dir: Path,
    evidence_graph: dict[str, Any],
) -> dict[str, Any]:
    playbook_root = playbook_dir.resolve()
    artifacts_root = package_dir / "evidence_artifacts"
    mapping: dict[str, str] = {}
    files: list[dict[str, Any]] = []

    for node in evidence_graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        paths = _node_keyframe_paths(node)
        for relative_path in paths:
            normalized = _normalize_relative_artifact_path(relative_path)
            if not normalized or normalized in mapping:
                continue
            source_path = (playbook_dir / normalized).resolve()
            try:
                source_path.relative_to(playbook_root)
            except ValueError:
                continue
            if not source_path.is_file():
                continue
            retained_relative = Path("evidence_artifacts") / normalized
            retained_path = package_dir / retained_relative
            retained_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, retained_path)
            retained_relative_posix = retained_relative.as_posix()
            mapping[normalized.as_posix()] = retained_relative_posix
            files.append(
                {
                    "sourceRelativePath": normalized.as_posix(),
                    "retainedRelativePath": retained_relative_posix,
                    "retainedPath": str(retained_path),
                    "sha256": _sha256_file(retained_path),
                }
            )

    if mapping:
        for node in evidence_graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            anchor = _normalize_relative_artifact_path(node.get("anchorKeyframePath"))
            if anchor and anchor.as_posix() in mapping:
                node["retainedAnchorKeyframePath"] = mapping[anchor.as_posix()]
            visual = node.get("visualEvidence")
            if isinstance(visual, dict):
                retained_paths = [
                    mapping[path.as_posix()]
                    for path in (
                        _normalize_relative_artifact_path(item)
                        for item in visual.get("keyframePaths") or []
                    )
                    if path and path.as_posix() in mapping
                ]
                if retained_paths:
                    visual["retainedKeyframePaths"] = retained_paths

    return {
        "artifactRoot": str(artifacts_root),
        "files": files,
    }


def _node_keyframe_paths(node: dict[str, Any]) -> list[Any]:
    paths: list[Any] = []
    if node.get("anchorKeyframePath"):
        paths.append(node.get("anchorKeyframePath"))
    visual = node.get("visualEvidence")
    if isinstance(visual, dict):
        paths.extend(visual.get("keyframePaths") or [])
    return paths


def _normalize_relative_artifact_path(value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _build_capability_ir(
    *,
    skill_name: str,
    shape: str,
    source_kind: str,
    playbook_name: str,
    manifest: dict[str, Any],
    sections: list[dict[str, Any]],
    evidence_graph: dict[str, Any],
    asset_path: str | Path,
    target_output_path: str | Path,
    rights_assessment: dict[str, Any] | None = None,
    source_security_assessment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    section_bindings = []
    for section, evidence in zip(sections, evidence_graph.get("nodes", [])):
        text = str(section.get("text") or section.get("captionText") or "")
        section_bindings.append(
            {
                "sectionOrdinal": section.get("ordinal"),
                "evidenceId": evidence["id"],
                "wordCount": int(section.get("wordCount") or len(text.split())),
                "summary": _truncate_words(text, 32),
            }
        )
    return {
        "schema": CAPABILITY_SCHEMA,
        "capabilityId": f"capability:{_slugify(skill_name)}",
        "skillName": skill_name,
        "shape": shape,
        "sourceKind": source_kind,
        "createdAt": _now(),
        "sourceBinding": {
            "playbookName": playbook_name,
            "sourceUrl": manifest.get("sourceUrl") or manifest.get("seedUrl"),
            "sectionBindings": section_bindings,
            "retainedArtifacts": evidence_graph.get("retainedArtifacts"),
        },
        "contract": {
            "inputs": {"request": "string", "source_context": "string | null"},
            "outputs": {
                "status": "'completed' | 'partial' | 'failed'",
                "citations": "list[string]",
                "artifact_paths": "list[string]",
            },
        },
        "execution": {
            "assetPath": str(asset_path),
            "targetOutputPath": str(target_output_path),
            "runtime": "agent-markdown",
            "requiresSourceCitations": True,
            "fixtures": _execution_fixtures(shape, section_bindings),
        },
        "canonical": {
            "isCanonical": True,
            "renderedTargetsAreDerived": True,
        },
        "risk": {
            "sideEffects": "unknown",
            "requiresHumanConfirmationForDestructiveActions": True,
            "stalenessRisk": "unknown",
            "rightsRisk": (rights_assessment or {}).get("fairUseRisk"),
            "sourceSecurityRisk": (source_security_assessment or {}).get("riskLevel"),
        },
        "rights": rights_assessment,
        "security": {
            "promptInjection": source_security_assessment,
        },
    }


def _score_certification(
    *,
    skill_name: str,
    shape: str,
    source_kind: str,
    capability: dict[str, Any],
    evidence: dict[str, Any],
    validators: list[dict[str, Any]],
    codify_result: dict[str, Any] | None,
    validation_result: dict[str, Any] | None,
    require_certification: bool,
) -> dict[str, Any]:
    validation_passed = bool(validation_result and validation_result.get("ok"))
    validation_total = 0
    validation_passed_count = 0
    if validation_result:
        validation_passed_count = int(validation_result.get("passed") or 0)
        validation_failed = int(validation_result.get("failed") or 0)
        validation_total = validation_passed_count + validation_failed

    dimensions = {
        "sourceFidelity": _score_source_fidelity(capability, evidence),
        "codification": 1.0 if codify_result and codify_result.get("ok") else 0.0,
        "executionValidation": (
            validation_passed_count / validation_total
            if validation_total
            else 1.0 if validation_passed
            else 0.0
        ),
        "governance": 1.0 if evidence.get("nodes") and capability.get("execution") else 0.5,
        "visualGrounding": _score_visual_grounding(source_kind, evidence),
        "validatorCoverage": _score_validator_coverage(validators),
        "domainCoverage": _score_category(validators, "domain_"),
        "capabilityIr": _score_category(validators, "capability_ir"),
        "rightsGovernance": _score_category(validators, "governance_rights"),
        "sourceSecurity": _score_category(validators, "governance_security"),
    }
    weights = {
        "sourceFidelity": 0.18,
        "codification": 0.10,
        "executionValidation": 0.18,
        "governance": 0.08,
        "visualGrounding": 0.08,
        "validatorCoverage": 0.08,
        "domainCoverage": 0.06,
        "capabilityIr": 0.06,
        "rightsGovernance": 0.08,
        "sourceSecurity": 0.10,
    }
    confidence = round(sum(dimensions[k] * weights[k] for k in weights), 3)
    critical_failures = [
        v for v in validators
        if v["severity"] == "critical" and not v["passed"]
    ]
    status = "certified" if confidence >= 0.75 and not critical_failures else "draft"
    if require_certification and status != "certified":
        status = "rejected"
    promotion_state = {
        "certified": "certified",
        "draft": "draft",
        "rejected": "rejected",
    }[status]
    return {
        "schema": CERTIFICATION_SCHEMA,
        "skillName": skill_name,
        "shape": shape,
        "sourceKind": source_kind,
        "status": status,
        "promotionState": promotion_state,
        "confidenceScore": confidence,
        "dimensions": dimensions,
        "validators": validators,
        "criticalFailures": critical_failures,
        "requiresCertification": require_certification,
        "createdAt": _now(),
    }


def _score_source_fidelity(capability: dict[str, Any], evidence: dict[str, Any]) -> float:
    sections = capability.get("sourceBinding", {}).get("sectionBindings", [])
    nodes = evidence.get("nodes", [])
    if not sections or not nodes:
        return 0.0
    bound = sum(1 for item in sections if item.get("evidenceId"))
    textful = sum(1 for item in sections if int(item.get("wordCount") or 0) > 0)
    return round(((bound / len(sections)) * 0.5) + ((textful / len(sections)) * 0.5), 3)


def _score_visual_grounding(source_kind: str, evidence: dict[str, Any]) -> float:
    if source_kind not in {"youtube_video", "local_video"}:
        return 1.0
    nodes = evidence.get("nodes", [])
    if not nodes:
        return 0.0
    visual_nodes = sum(
        1 for node in nodes
        if node.get("anchorKeyframePath")
        or (node.get("visualEvidence") or {}).get("keyframeCount")
    )
    action_nodes = sum(
        1 for node in nodes
        if node.get("visualActions")
        or (node.get("visualEvidence") or {}).get("visualActionCount")
    )
    keyframe_score = visual_nodes / len(nodes)
    action_score = action_nodes / len(nodes)
    return round((keyframe_score * 0.45) + (action_score * 0.55), 3)


def _score_validator_coverage(validators: list[dict[str, Any]]) -> float:
    actionable = [item for item in validators if not item.get("skipped")]
    if not actionable:
        return 0.0
    passed = sum(1 for item in actionable if item.get("passed"))
    return round(passed / len(actionable), 3)


def _score_category(validators: list[dict[str, Any]], prefix_or_name: str) -> float:
    items = [
        item for item in validators
        if not item.get("skipped")
        and str(item.get("category") or "").startswith(prefix_or_name)
    ]
    if not items:
        skipped = [
            item for item in validators
            if item.get("skipped")
            and str(item.get("category") or "").startswith(prefix_or_name)
        ]
        return 1.0 if skipped else 0.0
    scored = [float(item.get("score")) for item in items if item.get("score") is not None]
    if scored:
        return round(sum(scored) / len(scored), 3)
    passed = sum(1 for item in items if item.get("passed"))
    return round(passed / len(items), 3)


def _visual_evidence_for_section(
    section: dict[str, Any],
    step_evidence: list[dict[str, Any] | None],
) -> dict[str, Any]:
    keyframes = [
        step.get("keyframeRelativePath")
        for step in step_evidence
        if step and step.get("keyframeRelativePath")
    ]
    ocr_texts = [
        str(step.get("frameOcrText") or "")
        for step in step_evidence
        if step and step.get("frameOcrText")
    ]
    anchor = section.get("anchorKeyframePath")
    if anchor and anchor not in keyframes:
        keyframes.insert(0, str(anchor))
    return {
        "keyframeCount": len(keyframes),
        "keyframePaths": keyframes[:5],
        "ocrTextHash": _sha256_text("\n".join(ocr_texts)) if ocr_texts else None,
        "ocrWordCount": len(" ".join(ocr_texts).split()) if ocr_texts else 0,
        "visualActionCount": len(_visual_actions_for_section(section, step_evidence)),
    }


def _visual_actions_for_section(
    section: dict[str, Any],
    step_evidence: list[dict[str, Any] | None],
) -> list[dict[str, Any]]:
    actions = section.get("visualActions")
    if isinstance(actions, list) and actions:
        return actions[:20]
    out: list[dict[str, Any]] = []
    for step in step_evidence:
        if not step:
            continue
        action = step.get("visualAction")
        if not isinstance(action, dict):
            continue
        out.append(
            {
                "stepOrdinal": step.get("ordinal"),
                "actionType": action.get("actionType") or "unknown",
                "confidence": action.get("confidence"),
                "changedRatio": action.get("changedRatio"),
                "changedRegion": action.get("changedRegion"),
                "changedZones": action.get("changedZones") or [],
                "visibleTextSample": (action.get("ocr") or {}).get("visibleTextSample") or "",
                "observations": action.get("observations") or [],
            }
        )
    return out[:20]


def _execution_fixtures(shape: str, section_bindings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "skillmint.execution_fixtures.v1",
        "sampleInputs": {
            "request": "Apply the captured source-backed capability.",
            "source_context": "Use only the sandbox and cite source sections.",
        },
        "expectedOutputs": {
            "status": "completed | partial | failed",
            "citations": "non-empty list for source-backed claims",
        },
        "minimumEvidenceSections": min(1, len(section_bindings)),
        "rollbackRequiredForSideEffects": shape in {"skill", "workflow"},
    }


def _artifact_hashes(
    *,
    capability_path: Path,
    evidence_path: Path,
    certification_path: Path,
    asset_path: Path,
    target_output_path: Path,
) -> dict[str, str | None]:
    return {
        "capability": _sha256_file(capability_path),
        "evidence": _sha256_file(evidence_path),
        "certificationUnsigned": _sha256_file(certification_path),
        "asset": _sha256_file(asset_path),
        "targetOutput": _sha256_file(target_output_path),
    }


def _signature_for_certification(certification: dict[str, Any]) -> str:
    unsigned = dict(certification)
    unsigned.pop("signature", None)
    return _sha256_text(json.dumps(unsigned, sort_keys=True, default=str))


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_ledger_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    last = ""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
    except OSError:
        return None
    if not last:
        return None
    try:
        parsed = json.loads(last)
    except json.JSONDecodeError:
        return _sha256_text(last)
    return parsed.get("eventHash") or _sha256_text(last)


def _capability_package_dir(*, skills_root: str | Path | None, skill_name: str) -> Path:
    return _project_root(skills_root) / ".skillmint" / "capabilities" / _slugify(skill_name)


def _project_root(skills_root: str | Path | None) -> Path:
    if skills_root is None:
        return Path.cwd()
    root = Path(skills_root)
    parts = root.parts[-2:]
    if len(parts) == 2 and parts[-2] == ".claude" and parts[-1] in (
        "skills",
        "agents",
        "workflows",
    ):
        return root.parent.parent
    return root


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + "..."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
