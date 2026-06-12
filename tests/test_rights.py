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
