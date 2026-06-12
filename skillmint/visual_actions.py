"""Deterministic visual-action extraction for video keyframes."""
from __future__ import annotations

import hashlib
import re
from io import BytesIO
from typing import Any


VISUAL_ACTION_SCHEMA = "skillmint.visual_action.v1"


def analyze_visual_action(
    previous_jpeg: bytes | None,
    current_jpeg: bytes,
    *,
    diff_score: float | None = None,
    video_time_seconds: float | None = None,
    ocr_enabled: bool = True,
) -> dict[str, Any]:
    """Describe the visual action between two keyframes.

    This is intentionally deterministic and dependency-light. If PIL or OCR is
    unavailable, the result still records that visual analysis was attempted and
    why it was incomplete.
    """
    result: dict[str, Any] = {
        "schema": VISUAL_ACTION_SCHEMA,
        "videoTimeSeconds": video_time_seconds,
        "diffScore": diff_score,
        "actionType": "unknown",
        "confidence": 0.0,
        "changedRatio": None,
        "changedRegion": None,
        "changedZones": [],
        "ocr": {
            "attempted": bool(ocr_enabled),
            "available": False,
            "visibleTextHash": None,
            "visibleTextSample": "",
            "addedTextSample": "",
            "removedTextSample": "",
        },
        "observations": [],
    }
    try:
        from PIL import Image, ImageChops
    except Exception as exc:  # noqa: BLE001 - optional visual dependency.
        result["observations"].append(f"visual decode unavailable: {exc}")
        return result

    try:
        current = Image.open(BytesIO(current_jpeg)).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - malformed frame should not abort capture.
        result["observations"].append(f"current frame decode failed: {exc}")
        return result

    previous = None
    if previous_jpeg:
        try:
            previous = Image.open(BytesIO(previous_jpeg)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            result["observations"].append(f"previous frame decode failed: {exc}")

    current_text = _ocr_text(current) if ocr_enabled else ""
    previous_text = _ocr_text(previous) if ocr_enabled and previous is not None else ""
    _attach_ocr(result, current_text=current_text, previous_text=previous_text)

    if previous is None:
        result["actionType"] = "initial_view"
        result["confidence"] = 0.7 if current_text else 0.55
        result["observations"].append("initial keyframe establishes the visible state")
        return result

    if previous.size != current.size:
        previous = previous.resize(current.size)

    diff = ImageChops.difference(previous, current)
    bbox = diff.getbbox()
    width, height = current.size
    if not bbox:
        result["changedRatio"] = 0.0
        result["changedRegion"] = None
        result["actionType"] = "no_visible_change"
        result["confidence"] = 0.8
        result["observations"].append("no changed pixel region detected")
        return result

    left, top, right, bottom = bbox
    changed_area = max(0, right - left) * max(0, bottom - top)
    total_area = max(1, width * height)
    ratio = round(changed_area / total_area, 4)
    zones = _zones_for_bbox(bbox, width=width, height=height)
    result["changedRatio"] = ratio
    result["changedRegion"] = {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }
    result["changedZones"] = zones

    action_type, confidence, observations = _classify_action(
        changed_ratio=ratio,
        changed_zones=zones,
        current_text=current_text,
        previous_text=previous_text,
        diff_score=diff_score,
    )
    result["actionType"] = action_type
    result["confidence"] = confidence
    result["observations"].extend(observations)
    return result


def _attach_ocr(
    result: dict[str, Any],
    *,
    current_text: str,
    previous_text: str,
) -> None:
    ocr = result["ocr"]
    if current_text or previous_text:
        ocr["available"] = True
    if current_text:
        ocr["visibleTextHash"] = _sha256_text(current_text)
        ocr["visibleTextSample"] = _truncate_words(current_text, 40)
    added, removed = _text_delta(previous_text, current_text)
    ocr["addedTextSample"] = _truncate_words(" ".join(added), 24)
    ocr["removedTextSample"] = _truncate_words(" ".join(removed), 24)


def _ocr_text(image: Any | None) -> str:
    if image is None:
        return ""
    try:
        import pytesseract
    except Exception:
        return ""
    try:
        text = pytesseract.image_to_string(image)
    except Exception:
        return ""
    return _normalize_space(text)


def _zones_for_bbox(
    bbox: tuple[int, int, int, int],
    *,
    width: int,
    height: int,
) -> list[str]:
    left, top, right, bottom = bbox
    mid_x = (left + right) / 2
    mid_y = (top + bottom) / 2
    horizontal = "left" if mid_x < width / 3 else "right" if mid_x > width * 2 / 3 else "center"
    vertical = "top" if mid_y < height / 3 else "bottom" if mid_y > height * 2 / 3 else "middle"
    zones = [f"{vertical}_{horizontal}"]
    if top <= height * 0.18:
        zones.append("top_bar")
    if bottom >= height * 0.82:
        zones.append("bottom_bar")
    if left <= width * 0.18:
        zones.append("left_rail")
    if right >= width * 0.82:
        zones.append("right_rail")
    return sorted(set(zones))


def _classify_action(
    *,
    changed_ratio: float,
    changed_zones: list[str],
    current_text: str,
    previous_text: str,
    diff_score: float | None,
) -> tuple[str, float, list[str]]:
    observations: list[str] = []
    added, removed = _text_delta(previous_text, current_text)
    text_changed = bool(added or removed)
    if changed_ratio >= 0.45:
        observations.append("large screen replacement between keyframes")
        if text_changed:
            observations.append("visible OCR text changed")
        return "screen_transition", 0.82 if text_changed else 0.72, observations
    if "top_bar" in changed_zones or "left_rail" in changed_zones:
        observations.append("navigation or tool chrome changed")
        if text_changed:
            observations.append("visible labels changed")
        return "navigation_or_tool_change", 0.72 if text_changed else 0.62, observations
    if text_changed:
        observations.append("visible text/content changed")
        if changed_ratio <= 0.12:
            return "text_entry_or_selection", 0.74, observations
        return "content_update", 0.76, observations
    if changed_ratio <= 0.03:
        observations.append("small localized motion")
        return "pointer_or_selection_change", 0.5, observations
    if diff_score is not None and diff_score >= 60:
        observations.append("high frame diff without OCR evidence")
        return "visual_cut", 0.58, observations
    observations.append("moderate visual layout change")
    return "layout_change", 0.55, observations


def _text_delta(previous: str, current: str) -> tuple[list[str], list[str]]:
    prev_tokens = set(_tokens(previous))
    curr_tokens = set(_tokens(current))
    added = sorted(curr_tokens - prev_tokens)
    removed = sorted(prev_tokens - curr_tokens)
    return added[:20], removed[:20]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_\-]{2,}", text.lower())


def _normalize_space(text: str) -> str:
    return " ".join(str(text or "").split())


def _truncate_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + "..."


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
