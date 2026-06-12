"""Tests for deterministic visual-action extraction."""
from __future__ import annotations

from io import BytesIO

from PIL import Image

from skillmint.visual_actions import analyze_visual_action


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (64, 48)) -> bytes:
    image = Image.new("RGB", size, color)
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def test_visual_action_initial_view() -> None:
    action = analyze_visual_action(None, _jpeg((255, 0, 0)), ocr_enabled=False)

    assert action["schema"] == "skillmint.visual_action.v1"
    assert action["actionType"] == "initial_view"
    assert action["ocr"]["attempted"] is False


def test_visual_action_detects_screen_transition() -> None:
    action = analyze_visual_action(
        _jpeg((255, 0, 0)),
        _jpeg((0, 0, 255)),
        diff_score=95.0,
        ocr_enabled=False,
    )

    assert action["actionType"] == "screen_transition"
    assert action["changedRatio"] and action["changedRatio"] > 0.9
    assert action["confidence"] >= 0.7
