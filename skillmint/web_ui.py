"""Local web workbench for creating Skillmint playbooks and skills."""
from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any

from . import _claude_cli
from .rights import normalize_rights_basis
from .skill_creation import create_skill_from_source
from .skill_export import TARGET_ALIASES
from .tutorial_playbooks import _store_dir, list_tutorial_playbooks


MAX_BODY_BYTES = 256 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    input: dict[str, Any]
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    traceback: str | None = None


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def submit(self, payload: dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], input=payload)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job.id,), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = _now()
            payload = dict(job.input)
        try:
            kwargs = _create_kwargs(payload)
            result = create_skill_from_source(**kwargs)
        except Exception as exc:  # noqa: BLE001 - UI must surface user-actionable failures.
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = str(exc)
                job.traceback = traceback.format_exc()
                job.finished_at = _now()
            return
        with self._lock:
            job = self._jobs[job_id]
            job.status = "failed" if _result_failed(result) else "succeeded"
            job.result = result
            if job.status == "failed":
                job.error = _failed_result_error(result)
            job.finished_at = _now()


def _create_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    source = str(payload.get("source") or "").strip()
    skill_name = str(payload.get("skillName") or payload.get("skill_name") or "").strip()
    if not source:
        raise ValueError("source is required")

    shape = str(payload.get("shape") or "skill")
    if shape != "skill":
        raise ValueError(
            "certified GUI creation currently requires asset shape 'skill' because execution validation is required"
        )

    out: dict[str, Any] = {
        "source": source,
        "source_type": str(payload.get("sourceType") or "auto"),
        "target": str(payload.get("target") or "claude_code"),
        "shape": shape,
        "overwrite": _bool(payload.get("overwrite"), default=False),
        "codify": True,
        "validate": True,
        "require_certification": True,
        "keep_playbook": _bool(payload.get("keepPlaybook"), default=True),
    }
    if str(payload.get("codifyProvider") or "").strip().lower() in {"none", "off", "scaffold"}:
        raise ValueError("certified creation requires a finalization provider")
    out["codify_provider"] = str(payload.get("codifyProvider") or "deterministic")
    rights_basis = normalize_rights_basis(str(payload.get("rightsBasis") or ""))
    if rights_basis == "unknown":
        raise ValueError("certified creation requires a rights basis")
    out["rights_basis"] = rights_basis
    if skill_name:
        out["skill_name"] = skill_name
    optional_strings = {
        "playbookName": "playbook_name",
        "summary": "summary",
        "scopeNotes": "scope_notes",
        "ownerAgent": "owner_agent",
        "triggerDescription": "trigger_description",
        "skillsRoot": "skills_root",
        "urlPattern": "url_pattern",
        "captionsPath": "captions_path",
        "captionLanguage": "caption_language",
        "whisperModel": "whisper_model",
        "whisperDevice": "whisper_device",
        "sourceOwner": "source_owner",
        "sourceLicense": "source_license",
        "exportIntent": "export_intent",
    }
    for ui_key, api_key in optional_strings.items():
        value = payload.get(ui_key)
        if value not in (None, ""):
            out[api_key] = str(value)

    optional_ints = {
        "maxPages": "max_pages",
        "frameWidth": "frame_width",
        "maxHeight": "max_height",
    }
    for ui_key, api_key in optional_ints.items():
        value = _int_or_none(payload.get(ui_key))
        if value is not None:
            out[api_key] = value

    optional_floats = {
        "fps": "fps",
        "keyframeDiffThreshold": "keyframe_diff_threshold",
        "minStepSeconds": "min_step_seconds",
        "downloadTimeoutSeconds": "download_timeout_seconds",
        "processTimeoutSeconds": "process_timeout_seconds",
        "timeoutSeconds": "timeout_seconds",
        "sectionDiffScore": "section_diff_score",
        "codifyTimeoutSeconds": "codify_timeout_seconds",
        "validationTimeoutSeconds": "validation_timeout_seconds",
    }
    for ui_key, api_key in optional_floats.items():
        value = _float_or_none(payload.get(ui_key))
        if value is not None:
            out[api_key] = value

    page_start = _int_or_none(payload.get("pageStart"))
    page_end = _int_or_none(payload.get("pageEnd"))
    if page_start is not None or page_end is not None:
        if page_start is None or page_end is None:
            raise ValueError("page range requires both start and end")
        out["page_range"] = (page_start, page_end)

    if "sameOriginOnly" in payload:
        out["same_origin_only"] = _bool(payload.get("sameOriginOnly"), default=True)
    if "transcribe" in payload:
        out["transcribe"] = _bool(payload.get("transcribe"), default=True)
    if "ocr" in payload:
        out["ocr"] = _bool(payload.get("ocr"), default=False)
    if "renderJavascript" in payload:
        out["render_javascript"] = _bool(payload.get("renderJavascript"), default=False)
    if "keepValidationSandbox" in payload:
        out["keep_validation_sandbox"] = _bool(
            payload.get("keepValidationSandbox"),
            default=False,
        )
    if "commercialUseAllowed" in payload:
        out["commercial_use_allowed"] = _bool(
            payload.get("commercialUseAllowed"),
            default=False,
        )
    if "redistributionAllowed" in payload:
        out["redistribution_allowed"] = _bool(
            payload.get("redistributionAllowed"),
            default=False,
        )
    if isinstance(payload.get("captionLanguages"), list):
        out["caption_languages"] = tuple(str(x) for x in payload["captionLanguages"] if str(x).strip())
    return out


def _result_failed(result: dict[str, Any]) -> bool:
    return isinstance(result, dict) and result.get("ok") is False


def _failed_result_error(result: dict[str, Any]) -> str:
    certification_status = result.get("certificationStatus")
    confidence_score = result.get("confidenceScore")
    package = result.get("capabilityPackage") if isinstance(result.get("capabilityPackage"), dict) else {}
    certification = package.get("certification") if isinstance(package.get("certification"), dict) else {}
    failures = certification.get("criticalFailures") if isinstance(certification.get("criticalFailures"), list) else []
    failure_ids = [
        str(item.get("id"))
        for item in failures
        if isinstance(item, dict) and item.get("id")
    ][:5]

    parts = [
        "certification rejected"
        if certification_status == "rejected"
        else "skill creation returned ok=false"
    ]
    if certification_status:
        parts.append(f"status={certification_status}")
    if confidence_score is not None:
        parts.append(f"confidence={confidence_score}")
    if failure_ids:
        parts.append("criticalFailures=" + ", ".join(failure_ids))
    return "; ".join(parts)


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _job_payload(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "createdAt": job.created_at,
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "input": job.input,
        "result": job.result,
        "error": job.error,
        "traceback": job.traceback,
    }


class SkillmintHandler(BaseHTTPRequestHandler):
    store: JobStore

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        path = self.path.split("?", 1)[0]
        if path in ("", "/"):
            self._send_static("index.html", "text/html; charset=utf-8")
            return
        if path == "/styles.css":
            self._send_static("styles.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self._send_static("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/api/status":
            self._send_json(_status_payload())
            return
        if path == "/api/playbooks":
            self._send_json(list_tutorial_playbooks())
            return
        if path == "/api/jobs":
            self._send_json({"jobs": [_job_payload(job) for job in self.store.list()]})
            return
        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = self.store.get(job_id)
            if job is None:
                self._send_json({"ok": False, "error": "job not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(_job_payload(job))
            return
        self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        path = self.path.split("?", 1)[0]
        if path != "/api/create":
            self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            _create_kwargs(payload)
            job = self.store.submit(payload)
        except Exception as exc:  # noqa: BLE001 - return validation failure to UI.
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": True, "job": _job_payload(job)}, HTTPStatus.ACCEPTED)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        raw_len = int(self.headers.get("Content-Length") or "0")
        if raw_len > MAX_BODY_BYTES:
            raise ValueError("request body is too large")
        body = self.rfile.read(raw_len)
        if not body:
            return {}
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return parsed

    def _send_static(self, name: str, content_type: str) -> None:
        resource = files("skillmint.ui").joinpath(name)
        body = resource.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _status_payload() -> dict[str, Any]:
    try:
        claude_path = _claude_cli.ensure_available()
        claude = {"available": True, "path": claude_path, "error": None}
    except Exception as exc:  # noqa: BLE001 - status endpoint should not crash.
        claude = {"available": False, "path": None, "error": str(exc)}
    targets = sorted(set(TARGET_ALIASES.values()))
    return {
        "ok": True,
        "playbookRoot": str(_store_dir()),
        "targets": targets,
        "claudeCli": claude,
    }


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    SkillmintHandler.store = JobStore()
    return ThreadingHTTPServer((host, port), SkillmintHandler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the local Skillmint web UI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="Open the UI in your default browser.")
    args = parser.parse_args(argv)

    try:
        httpd = make_server(args.host, args.port)
    except OSError:
        if args.port != DEFAULT_PORT:
            raise
        httpd = make_server(args.host, 0)
    actual_port = httpd.server_address[1]
    url = f"http://{args.host}:{actual_port}"
    print(f"Skillmint UI running at {url}")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
