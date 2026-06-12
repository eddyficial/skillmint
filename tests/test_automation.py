"""Tests for the no-UI automation command."""
from __future__ import annotations

import json
from typing import Any

from skillmint import automation


def test_run_calls_create_with_source_only(capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_create(source: str, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return {
            "ok": True,
            "skillName": "youtube-abc123",
            "skillNameInferred": True,
            "outputPath": "SKILL.md",
        }

    code = automation.run(
        [
            "https://youtu.be/abc123",
            "--target",
            "codex",
            "--shape",
            "skill",
            "--no-codify",
        ],
        create_fn=fake_create,
    )

    assert code == 0
    assert captured["source"] == "https://youtu.be/abc123"
    assert captured["skill_name"] is None
    assert captured["target"] == "codex"
    assert captured["codify"] is False
    assert captured["codify_provider"] == "deterministic"
    assert captured["validate"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["skillNameInferred"] is True


def test_run_can_select_claude_cli_provider(capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_create(source: str, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return {"ok": True, "skillName": "demo", "outputPath": "SKILL.md"}

    code = automation.run(
        [
            "https://example.com/tutorial",
            "--codify-provider",
            "claude_cli",
        ],
        create_fn=fake_create,
    )

    assert code == 0
    assert captured["codify"] is True
    assert captured["codify_provider"] == "claude_cli"
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_run_can_enable_validation(capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_create(source: str, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return {"ok": True, "skillName": "demo", "validated": True}

    code = automation.run(
        [
            "https://example.com/tutorial",
            "--validate",
            "--validation-timeout-seconds",
            "42",
            "--keep-validation-sandbox",
            "--require-certification",
        ],
        create_fn=fake_create,
    )

    assert code == 0
    assert captured["validate"] is True
    assert captured["validation_timeout_seconds"] == 42.0
    assert captured["keep_validation_sandbox"] is True
    assert captured["require_certification"] is True
    assert json.loads(capsys.readouterr().out)["validated"] is True


def test_run_threads_visual_capture_flags(capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_create(source: str, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return {"ok": True, "skillName": "demo"}

    code = automation.run(
        [
            "https://example.com/tutorial",
            "--ocr",
            "--render-javascript",
        ],
        create_fn=fake_create,
    )

    assert code == 0
    assert captured["ocr"] is True
    assert captured["render_javascript"] is True
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_run_threads_rights_flags(capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_create(source: str, **kwargs):
        captured["source"] = source
        captured.update(kwargs)
        return {"ok": True, "skillName": "demo"}

    code = automation.run(
        [
            "https://example.com/tutorial",
            "--rights-basis",
            "owned",
            "--source-owner",
            "Acme",
            "--source-license",
            "internal",
            "--commercial-use-allowed",
            "--redistribution-allowed",
            "--export-intent",
            "commercial",
        ],
        create_fn=fake_create,
    )

    assert code == 0
    assert captured["rights_basis"] == "owned"
    assert captured["source_owner"] == "Acme"
    assert captured["source_license"] == "internal"
    assert captured["commercial_use_allowed"] is True
    assert captured["redistribution_allowed"] is True
    assert captured["export_intent"] == "commercial"
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_page_range_parser_accepts_dash_or_colon() -> None:
    assert automation._page_range("2-6") == (2, 6)
    assert automation._page_range("2:6") == (2, 6)
