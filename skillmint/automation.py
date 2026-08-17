"""No-UI automation entrypoint for source-to-skill creation."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from .skill_creation import create_skill_from_source


CreateFn = Callable[..., dict[str, Any]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillmint-create",
        description="Create a Skillmint playbook and skill from one source.",
    )
    parser.add_argument("source", help="YouTube URL, docs URL, PDF URL/path, web page, or video path.")
    parser.add_argument("--skill-name", help="Override the inferred skill name.")
    parser.add_argument("--playbook-name", help="Override the playbook name.")
    parser.add_argument("--source-type", default="auto")
    parser.add_argument("--target", default="claude_code")
    parser.add_argument("--shape", default="skill")
    parser.add_argument("--project-root", dest="skills_root")
    parser.add_argument("--summary")
    parser.add_argument("--scope-notes")
    parser.add_argument("--trigger-description")
    parser.add_argument("--owner-agent")
    parser.add_argument("--rights-basis", default="unknown")
    parser.add_argument("--source-owner")
    parser.add_argument("--source-license")
    parser.add_argument("--commercial-use-allowed", action="store_true")
    parser.add_argument("--redistribution-allowed", action="store_true")
    parser.add_argument(
        "--export-intent",
        default="private",
        choices=["private", "internal", "public", "commercial"],
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--codify-provider",
        default="deterministic",
        choices=["deterministic", "claude_cli", "none"],
        help="Provider for scaffold finalization. Default does not use AI.",
    )
    parser.add_argument("--no-codify", dest="codify", action="store_false")
    parser.set_defaults(codify=True)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--url-pattern")
    parser.add_argument("--page-range", type=_page_range)
    parser.add_argument("--ocr", action="store_true", help="OCR PDF pages with no embedded text.")
    parser.add_argument(
        "--render-javascript",
        action="store_true",
        help="Render single web pages with Playwright before extracting content.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--codify-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--validate", action="store_true", help="Run validate_skill after codification.")
    parser.add_argument("--validation-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--keep-validation-sandbox", action="store_true")
    parser.add_argument(
        "--require-certification",
        action="store_true",
        help="Return ok=false unless certification gates pass.",
    )
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--frame-width", type=int, default=480)
    parser.add_argument("--caption-language", default="en")
    parser.add_argument("--caption-languages", default="en")
    parser.add_argument("--captions-path")
    parser.add_argument("--no-transcribe", dest="transcribe", action="store_false")
    parser.set_defaults(transcribe=True)
    return parser


def run(
    argv: list[str] | None = None,
    *,
    create_fn: CreateFn = create_skill_from_source,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    caption_languages = tuple(
        item.strip()
        for item in str(args.caption_languages or "").split(",")
        if item.strip()
    ) or ("en",)
    try:
        result = create_fn(
            args.source,
            skill_name=args.skill_name,
            source_type=args.source_type,
            playbook_name=args.playbook_name,
            summary=args.summary,
            shape=args.shape,
            trigger_description=args.trigger_description,
            scope_notes=args.scope_notes,
            owner_agent=args.owner_agent,
            rights_basis=args.rights_basis,
            source_owner=args.source_owner,
            source_license=args.source_license,
            commercial_use_allowed=args.commercial_use_allowed or None,
            redistribution_allowed=args.redistribution_allowed or None,
            export_intent=args.export_intent,
            overwrite=args.overwrite,
            skills_root=args.skills_root,
            target=args.target,
            codify=args.codify,
            codify_provider=args.codify_provider,
            codify_timeout_seconds=args.codify_timeout_seconds,
            validate=args.validate,
            validation_timeout_seconds=args.validation_timeout_seconds,
            keep_validation_sandbox=args.keep_validation_sandbox,
            require_certification=args.require_certification,
            fps=args.fps,
            frame_width=args.frame_width,
            caption_languages=caption_languages,
            captions_path=args.captions_path,
            caption_language=args.caption_language,
            transcribe=args.transcribe,
            page_range=args.page_range,
            ocr=args.ocr,
            max_pages=args.max_pages,
            url_pattern=args.url_pattern,
            timeout_seconds=args.timeout_seconds,
            render_javascript=args.render_javascript,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should return a machine-readable error.
        payload = {
            "ok": False,
            "error": str(exc),
            "errorType": type(exc).__name__,
        }
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    # A gate can fail (e.g. --require-certification rejects the skill) without
    # raising — create_fn returns ok=False instead of throwing. The process
    # exit code must reflect that, or a caller checking $? instead of parsing
    # the JSON body sees "success" for a rejected/failed result.
    return 0 if result.get("ok") else 1


def _page_range(raw: str) -> tuple[int, int]:
    parts = [part.strip() for part in raw.replace(":", "-").split("-") if part.strip()]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected START-END")
    start, end = int(parts[0]), int(parts[1])
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError("expected 1-based START-END with END >= START")
    return (start, end)


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
