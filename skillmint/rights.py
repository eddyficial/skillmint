"""Rights and provenance policy for source-to-capability generation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RIGHTS_SCHEMA = "skillmint.rights_assessment.v1"

_BASIS_ALIASES = {
    "": "unknown",
    "unknown": "unknown",
    "unspecified": "unknown",
    "owned": "owned",
    "owner": "owned",
    "user_owned": "owned",
    "licensed": "licensed",
    "license": "licensed",
    "permission": "user_attested_permission",
    "permitted": "user_attested_permission",
    "user_attested_permission": "user_attested_permission",
    "creative_commons": "creative_commons",
    "cc": "creative_commons",
    "public_domain": "public_domain",
    "pd": "public_domain",
    "internal": "internal",
    "internal_sop": "internal",
    "fair_use": "fair_use",
}

_INTENT_ALIASES = {
    "": "private",
    "private": "private",
    "personal": "private",
    "local": "private",
    "internal": "internal",
    "team": "internal",
    "public": "public",
    "publish": "public",
    "redistribute": "public",
    "commercial": "commercial",
}

_LOW_RISK_BASES = {"owned", "licensed", "public_domain", "internal"}
_MEDIUM_RISK_BASES = {"creative_commons", "user_attested_permission", "fair_use"}
_KNOWN_BASES = _LOW_RISK_BASES | _MEDIUM_RISK_BASES


class RightsPolicyError(RuntimeError):
    """Raised when requested export violates the rights assessment."""


def assess_rights(
    *,
    source_kind: str,
    source_url: str | None = None,
    source_owner: str | None = None,
    rights_basis: str | None = None,
    source_license: str | None = None,
    commercial_use_allowed: bool | None = None,
    redistribution_allowed: bool | None = None,
    contains_verbatim_source: bool | None = None,
    full_transcript_stored: bool | None = None,
    export_intent: str | None = None,
    manifest: dict[str, Any] | None = None,
    lessons: dict[str, Any] | None = None,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic rights assessment for a generated capability."""
    manifest = manifest or {}
    lessons = lessons or {}
    basis = normalize_rights_basis(rights_basis)
    intent = normalize_export_intent(export_intent)
    inferred_source_url = (
        source_url
        or manifest.get("sourceUrl")
        or manifest.get("seedUrl")
        or (manifest.get("captureConfig") or {}).get("originalUrl")
    )
    owner = source_owner or (manifest.get("video") or {}).get("channel")
    license_name = source_license or str(manifest.get("license") or "").strip() or None
    section_words = _section_word_count(lessons)
    verbatim_words = _estimate_verbatim_words(asset_path, lessons)
    if contains_verbatim_source is None:
        contains_verbatim_source = verbatim_words > 0
    if full_transcript_stored is None:
        full_transcript_stored = source_kind in {"youtube_video", "local_video"}

    commercial_allowed = _resolve_commercial_allowed(
        basis,
        explicit=commercial_use_allowed,
        license_name=license_name,
    )
    redistribution = _resolve_redistribution_allowed(
        basis,
        explicit=redistribution_allowed,
        license_name=license_name,
    )
    risk, reasons = _risk_level(
        source_kind=source_kind,
        basis=basis,
        intent=intent,
        commercial_use_allowed=commercial_allowed,
        redistribution_allowed=redistribution,
        contains_verbatim_source=bool(contains_verbatim_source),
        verbatim_words=verbatim_words,
        full_transcript_stored=bool(full_transcript_stored),
    )
    export_allowed = _export_allowed(
        intent=intent,
        risk=risk,
        basis=basis,
        commercial_use_allowed=commercial_allowed,
        redistribution_allowed=redistribution,
    )
    review_required = (
        risk in {"medium", "high"}
        or basis in {"unknown", "fair_use"}
        or intent in {"public", "commercial"}
    )

    return {
        "schema": RIGHTS_SCHEMA,
        "sourceKind": source_kind,
        "sourceUrl": inferred_source_url,
        "sourceOwner": owner,
        "rightsBasis": basis,
        "license": license_name,
        "commercialUseAllowed": commercial_allowed,
        "redistributionAllowed": redistribution,
        "exportIntent": intent,
        "exportAllowed": export_allowed,
        "fairUseRisk": risk,
        "reviewRequired": review_required,
        "containsVerbatimSource": bool(contains_verbatim_source),
        "estimatedVerbatimSourceWords": verbatim_words,
        "capturedSourceWordCount": section_words,
        "fullTranscriptStored": bool(full_transcript_stored),
        "riskReasons": reasons,
    }


def assert_export_allowed(assessment: dict[str, Any]) -> None:
    """Raise when the requested export intent is not permitted.

    ``exportAllowed`` is ``True`` (fully allowed), ``False`` (blocked), or the
    string ``"private_only"`` (capped to private/internal use because of
    elevated risk). A "private_only" cap is not a block when the caller
    already requested private or internal export — that's exactly what the
    cap allows. It only blocks a request for something broader (public or
    commercial) than the risk assessment permits.
    """
    intent = assessment.get("exportIntent")
    allowed = assessment.get("exportAllowed")
    if allowed is True:
        return
    if allowed == "private_only" and intent in {"private", "internal"}:
        return
    basis = assessment.get("rightsBasis")
    reasons = "; ".join(assessment.get("riskReasons") or [])
    raise RightsPolicyError(
        f"rights gate blocked {intent} export; exportAllowed={allowed}, "
        f"rightsBasis={basis}. {reasons}"
    )


def normalize_rights_basis(value: str | None) -> str:
    key = (value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    return _BASIS_ALIASES.get(key, "unknown")


def normalize_export_intent(value: str | None) -> str:
    key = (value or "private").strip().lower().replace("-", "_").replace(" ", "_")
    return _INTENT_ALIASES.get(key, "private")


def _resolve_commercial_allowed(
    basis: str,
    *,
    explicit: bool | None,
    license_name: str | None,
) -> bool:
    if explicit is not None:
        return bool(explicit)
    if basis in {"owned", "licensed", "public_domain"}:
        return True
    if basis == "internal":
        return False
    if basis == "creative_commons":
        return bool(license_name and "-nc" not in license_name.lower())
    return False


def _resolve_redistribution_allowed(
    basis: str,
    *,
    explicit: bool | None,
    license_name: str | None,
) -> bool:
    if explicit is not None:
        return bool(explicit)
    if basis in {"owned", "licensed", "public_domain"}:
        return True
    if basis == "creative_commons":
        return bool(license_name)
    return False


def _risk_level(
    *,
    source_kind: str,
    basis: str,
    intent: str,
    commercial_use_allowed: bool,
    redistribution_allowed: bool,
    contains_verbatim_source: bool,
    verbatim_words: int,
    full_transcript_stored: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    points = 0
    if basis == "unknown":
        points += 4
        reasons.append("rights basis is unknown")
    elif basis in _MEDIUM_RISK_BASES:
        points += 2
        reasons.append(f"rights basis requires review: {basis}")
    elif basis not in _KNOWN_BASES:
        points += 4
        reasons.append(f"rights basis is unsupported: {basis}")

    if source_kind == "youtube_video" and basis not in _LOW_RISK_BASES:
        points += 2
        reasons.append("YouTube/public video sources require explicit rights for reuse")
    if intent == "commercial" and not commercial_use_allowed:
        points += 4
        reasons.append("commercial use is not allowed by the assessment")
    if intent == "public" and not redistribution_allowed:
        points += 3
        reasons.append("redistribution is not allowed by the assessment")
    if contains_verbatim_source:
        points += 2
        reasons.append(f"generated asset may contain source expression ({verbatim_words} estimated words)")
    if full_transcript_stored and basis not in _LOW_RISK_BASES:
        points += 1
        reasons.append("full transcript retention should be minimized or licensed")

    if points >= 6:
        return "high", reasons
    if points >= 3:
        return "medium", reasons
    return "low", reasons


def _export_allowed(
    *,
    intent: str,
    risk: str,
    basis: str,
    commercial_use_allowed: bool,
    redistribution_allowed: bool,
) -> bool | str:
    if intent in {"private", "internal"}:
        return True if risk != "high" else "private_only"
    if intent == "commercial":
        return commercial_use_allowed and redistribution_allowed and risk == "low"
    if intent == "public":
        return redistribution_allowed and risk in {"low", "medium"} and basis != "fair_use"
    return "private_only"


def _section_word_count(lessons: dict[str, Any]) -> int:
    total = 0
    for section in lessons.get("sections") or []:
        if section.get("wordCount") is not None:
            total += int(section.get("wordCount") or 0)
        else:
            total += len(str(section.get("text") or section.get("captionText") or "").split())
    return total


def _estimate_verbatim_words(asset_path: str | Path | None, lessons: dict[str, Any]) -> int:
    if not asset_path:
        return 0
    path = Path(asset_path)
    if not path.is_file():
        return 0
    try:
        asset_text = path.read_text(encoding="utf-8").lower()
    except OSError:
        return 0
    total = 0
    for section in lessons.get("sections") or []:
        source_text = str(section.get("text") or section.get("captionText") or "")
        for phrase in _long_phrases(source_text):
            if phrase.lower() in asset_text:
                total += len(phrase.split())
    return total


def _long_phrases(text: str, *, phrase_words: int = 12, limit: int = 4) -> list[str]:
    words = text.split()
    phrases = []
    for idx in range(0, max(0, len(words) - phrase_words + 1), phrase_words):
        phrase = " ".join(words[idx: idx + phrase_words])
        if phrase:
            phrases.append(phrase)
        if len(phrases) >= limit:
            break
    return phrases


def assessment_from_file(path: str | Path) -> dict[str, Any]:
    """Read a persisted rights assessment."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
