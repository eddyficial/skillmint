"""Tests for rights and provenance assessment."""
from __future__ import annotations

import pytest

from skillmint.rights import RightsPolicyError, assert_export_allowed, assess_rights


def test_owned_source_allows_commercial_export() -> None:
    assessment = assess_rights(
        source_kind="pdf",
        rights_basis="owned",
        source_owner="Acme",
        export_intent="commercial",
        lessons={"sections": [{"text": "Run the internal process.", "wordCount": 4}]},
    )

    assert assessment["rightsBasis"] == "owned"
    assert assessment["commercialUseAllowed"] is True
    assert assessment["redistributionAllowed"] is True
    assert assessment["exportAllowed"] is True
    assert assessment["fairUseRisk"] == "low"


def test_unknown_youtube_source_blocks_public_export() -> None:
    assessment = assess_rights(
        source_kind="youtube_video",
        rights_basis="unknown",
        export_intent="public",
        lessons={"sections": [{"text": "Do this exact public tutorial step.", "wordCount": 7}]},
    )

    assert assessment["exportAllowed"] in (False, "private_only")
    assert assessment["fairUseRisk"] == "high"
    with pytest.raises(RightsPolicyError, match="rights gate blocked public export"):
        assert_export_allowed(assessment)


def test_high_risk_youtube_source_still_allows_private_export() -> None:
    """Regression: a real third-party YouTube tutorial captured under
    ``user_attested_permission`` — the rights basis the README itself
    recommends for such sources — scores as "high" risk (medium-risk basis +
    youtube_video + verbatim echo + full transcript retention). That used to
    hard-block export even though the caller only asked for private use,
    which is exactly what a "high" risk verdict still permits. Requesting
    private/internal export must succeed whenever the assessment caps out at
    "private_only" rather than a real block.
    """
    assessment = assess_rights(
        source_kind="youtube_video",
        rights_basis="user_attested_permission",
        source_owner="Some Creator",
        export_intent="private",
        contains_verbatim_source=True,
        lessons={
            "sections": [
                {"text": "Step one. Cross the wide end over the narrow end.", "wordCount": 9}
            ]
        },
    )

    assert assessment["fairUseRisk"] == "high"
    assert assessment["exportAllowed"] == "private_only"
    # Must not raise: the request already matches what "private_only" allows.
    assert_export_allowed(assessment)


def test_private_only_cap_still_blocks_a_broader_requested_intent() -> None:
    """A "private_only" cap must still block when the recorded intent is
    broader than private/internal — the fix only unblocks the case where the
    request already matches the cap, it must not become a blanket bypass.
    """
    assessment = {
        "exportIntent": "public",
        "exportAllowed": "private_only",
        "rightsBasis": "user_attested_permission",
        "riskReasons": ["YouTube/public video sources require explicit rights for reuse"],
    }

    with pytest.raises(RightsPolicyError, match="rights gate blocked public export"):
        assert_export_allowed(assessment)
