"""Tests for the local Skillmint web UI adapter."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from skillmint import web_ui
from skillmint.web_ui import _create_kwargs, make_server


def test_create_kwargs_maps_ui_payload() -> None:
    kwargs = _create_kwargs(
        {
            "source": "https://example.com/docs",
            "skillName": "demo-skill",
            "sourceType": "documentation_site",
            "target": "codex",
            "codifyProvider": "claude_cli",
            "shape": "skill",
            "overwrite": "true",
            "codify": False,
            "validate": False,
            "keepPlaybook": False,
            "requireCertification": False,
            "validationTimeoutSeconds": "44",
            "keepValidationSandbox": "true",
            "ocr": "true",
            "renderJavascript": "true",
            "maxPages": "12",
            "pageStart": "2",
            "pageEnd": "6",
            "captionLanguages": ["en", "es"],
            "rightsBasis": "owned",
            "sourceOwner": "Acme",
            "sourceLicense": "internal",
            "commercialUseAllowed": True,
            "redistributionAllowed": True,
            "exportIntent": "commercial",
        }
    )

    assert kwargs["source"] == "https://example.com/docs"
    assert kwargs["skill_name"] == "demo-skill"
    assert kwargs["source_type"] == "documentation_site"
    assert kwargs["target"] == "codex"
    assert kwargs["codify_provider"] == "claude_cli"
    assert kwargs["shape"] == "skill"
    assert kwargs["overwrite"] is True
    assert kwargs["codify"] is True
    assert kwargs["validate"] is True
    assert kwargs["require_certification"] is True
    assert kwargs["keep_playbook"] is False
    assert kwargs["validation_timeout_seconds"] == 44.0
    assert kwargs["keep_validation_sandbox"] is True
    assert kwargs["ocr"] is True
    assert kwargs["render_javascript"] is True
    assert kwargs["max_pages"] == 12
    assert kwargs["page_range"] == (2, 6)
    assert kwargs["caption_languages"] == ("en", "es")
    assert kwargs["rights_basis"] == "owned"
    assert kwargs["source_owner"] == "Acme"
    assert kwargs["source_license"] == "internal"
    assert kwargs["commercial_use_allowed"] is True
    assert kwargs["redistribution_allowed"] is True
    assert kwargs["export_intent"] == "commercial"


def test_create_kwargs_requires_complete_page_range() -> None:
    with pytest.raises(ValueError, match="page range requires both start and end"):
        _create_kwargs(
            {
                "source": "manual.pdf",
                "skillName": "manual-skill",
                "rightsBasis": "owned",
                "pageStart": "2",
            }
        )


def test_create_kwargs_allows_source_only_automation() -> None:
    kwargs = _create_kwargs(
        {
            "source": "https://example.com/tutorial",
            "rightsBasis": "owned",
        }
    )

    assert kwargs["source"] == "https://example.com/tutorial"
    assert "skill_name" not in kwargs
    assert kwargs["source_type"] == "auto"
    assert kwargs["codify"] is True
    assert kwargs["validate"] is True
    assert kwargs["require_certification"] is True


def test_create_kwargs_requires_rights_basis_for_certified_gui_creation() -> None:
    with pytest.raises(ValueError, match="rights basis"):
        _create_kwargs({"source": "https://example.com/tutorial"})


def test_create_kwargs_rejects_disabled_finalization_provider() -> None:
    with pytest.raises(ValueError, match="finalization provider"):
        _create_kwargs(
            {
                "source": "https://example.com/tutorial",
                "rightsBasis": "owned",
                "codifyProvider": "none",
            }
        )


def test_create_kwargs_rejects_unvalidated_shapes_for_certified_gui_creation() -> None:
    with pytest.raises(ValueError, match="asset shape 'skill'"):
        _create_kwargs(
            {
                "source": "https://example.com/tutorial",
                "rightsBasis": "owned",
                "shape": "agent",
            }
        )


def test_web_server_status_and_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path / "playbooks"))
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with urllib.request.urlopen(f"{base_url}/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "Source in. Skill out. Playbook optional." in html
        assert "Keep playbook" in html
        assert "Rights basis" in html
        assert "Validate and certify required" not in html
        assert '<option value="agent">' not in html

        with urllib.request.urlopen(f"{base_url}/app.js", timeout=5) as response:
            script = response.read().decode("utf-8")
        assert "createForm" in script
        assert "Claude CLI is required for certified GUI creation." in script

        with urllib.request.urlopen(f"{base_url}/api/status", timeout=5) as response:
            status = json.loads(response.read().decode("utf-8"))
        assert status["ok"] is True
        assert status["playbookRoot"] == str(tmp_path / "playbooks")
        assert "claude_code" in status["targets"]

        request = urllib.request.Request(
            f"{base_url}/api/create",
            data=json.dumps({"skillName": "missing-source"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_web_job_rejected_certification_is_failed_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILLMINT_PLAYBOOK_DIR", str(tmp_path / "playbooks"))

    def fake_create_skill_from_source(**kwargs):
        return {
            "ok": False,
            "certificationStatus": "rejected",
            "confidenceScore": 0.42,
            "capabilityPackage": {
                "certification": {
                    "criticalFailures": [
                        {"id": "execution_validation_passed"},
                    ],
                },
            },
        }

    monkeypatch.setattr(web_ui, "create_skill_from_source", fake_create_skill_from_source)
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        request = urllib.request.Request(
            f"{base_url}/api/create",
            data=json.dumps(
                {
                    "source": "https://example.com/tutorial",
                    "skillName": "demo-skill",
                    "rightsBasis": "owned",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            created = json.loads(response.read().decode("utf-8"))
        job_id = created["job"]["id"]

        job = None
        for _ in range(30):
            with urllib.request.urlopen(f"{base_url}/api/jobs/{job_id}", timeout=5) as response:
                job = json.loads(response.read().decode("utf-8"))
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)

        assert job is not None
        assert job["status"] == "failed"
        assert job["result"]["ok"] is False
        assert "certification rejected" in job["error"]
        assert "execution_validation_passed" in job["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
