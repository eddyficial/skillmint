"""End-to-end smoke test: capture (4 sources) -> distill -> compose -> inspect.

Runs against the CURRENT on-disk code (bypasses any pinned MCP subprocess).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from skillmint.document_capture import (
    capture_documentation_site_to_playbook,
    capture_pdf_to_playbook,
    capture_web_page_to_playbook,
)
from skillmint.offline_video_capture import capture_youtube_video_to_playbook
from skillmint.skill_synthesis import compose_skill_scaffold_from_playbook
from skillmint.tutorial_playbooks import distill_tutorial_playbook

_BULLET_RE = re.compile(r"^- \*\*§\d+\s")  # "- **§N "

TARGETS = {
    "youtube": {
        "fn": capture_youtube_video_to_playbook,
        "kwargs": {"url": "https://www.youtube.com/watch?v=Gjnup-PuquQ", "name": "smoketest-yt", "overwrite": True},
        "expected_source_marker": "**Video:**",
        "expected_label_pattern": r"§\d+ \(\d+:\d{2}",  # §N (m:ss or h:mm:ss
    },
    "web": {
        "fn": capture_web_page_to_playbook,
        "kwargs": {"url": "https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Methods", "name": "smoketest-web", "overwrite": True},
        "expected_source_marker": "**Source page:**",
        "expected_label_pattern": r"§\d+ — \S",  # §N — <heading>, no timestamp parens
    },
    "pdf": {
        "fn": capture_pdf_to_playbook,
        "kwargs": {"path": r"C:\path\to\a\local\sample.pdf", "name": "smoketest-pdf", "overwrite": True},
        "expected_source_marker": "**Source PDF:**",
        "expected_label_pattern": r"\(page \d+\)",
    },
    "docs": {
        "fn": capture_documentation_site_to_playbook,
        "kwargs": {"url": "https://docs.astral.sh/uv/getting-started/", "name": "smoketest-docs", "max_pages": 3, "overwrite": True},
        "expected_source_marker": "**Source docs root:**",
        "expected_label_pattern": r"§\d+ — \S",
    },
}


def run_one(label: str, spec: dict, skills_root: Path) -> dict:
    name = spec["kwargs"]["name"]
    out: dict = {"label": label, "playbook": name}
    t0 = time.time()
    try:
        cap = spec["fn"](**spec["kwargs"])
        out["capture_ms"] = round((time.time() - t0) * 1000)
        out["step_count"] = cap.get("stepCount")
        cfg = cap.get("captureConfig") or {}
        out["source_kind_raw"] = cfg.get("sourceKind")  # None for YouTube by design
    except Exception as e:
        out["capture_error"] = f"{type(e).__name__}: {e}"
        return out

    t1 = time.time()
    try:
        dist = distill_tutorial_playbook(name)
        out["distill_ms"] = round((time.time() - t1) * 1000)
        out["section_count"] = dist.get("sectionCount")
    except Exception as e:
        out["distill_error"] = f"{type(e).__name__}: {e}"
        return out

    t2 = time.time()
    try:
        skill_name = f"smoketest-{label}"
        comp = compose_skill_scaffold_from_playbook(
            playbook_name=name,
            skill_name=skill_name,
            overwrite=True,
            skills_root=str(skills_root),
        )
        out["compose_ms"] = round((time.time() - t2) * 1000)
        skill_path = Path(comp["skillPath"])
        out["skill_path"] = str(skill_path)
        out["word_count"] = comp.get("wordCount")
        out["critical_rule"] = comp.get("criticalRule")
        out["next_step"] = comp.get("nextStep")
        body = skill_path.read_text(encoding="utf-8")

        # Inspect the SKILL.md
        desc_line = next((ln for ln in body.splitlines() if ln.startswith("description:")), "")
        out["description"] = desc_line[len("description:"):].strip().strip('"')

        # Lesson section bullets only (skip the header block bullets like **Playbook:** / **Video:** / **Source URL:**)
        section_lines = [ln for ln in body.splitlines() if _BULLET_RE.match(ln)]
        out["first_section_labels"] = [ln[:140] for ln in section_lines[:3]]
        out["section_bullet_count"] = len(section_lines)

        # Source block (Video / Source page / Source PDF / Source docs root)
        source_line = next((ln for ln in body.splitlines() if any(k in ln for k in ("**Video:**", "**Source page:**", "**Source PDF:**", "**Source docs root:**"))), "")
        out["source_block"] = source_line.strip()

        # Validate against expectations
        expected_marker = spec["expected_source_marker"]
        if expected_marker not in source_line:
            out["validation_error"] = f"expected '{expected_marker}' in source block, got: {source_line!r}"
        elif not section_lines:
            out["validation_error"] = "no section bullets found in SKILL.md"
        else:
            label_pat = re.compile(spec["expected_label_pattern"])
            if not any(label_pat.search(ln) for ln in section_lines[:5]):
                out["validation_error"] = f"no section label matched {spec['expected_label_pattern']!r}; first label: {section_lines[0]!r}"
        # The bug we previously had: ?:?? on non-video sources. Check that none appear.
        if expected_marker != "**Video:**" and "?:??" in body:
            out["validation_error"] = "non-video SKILL.md contains '?:??' timestamp placeholder"
    except Exception as e:
        out["compose_error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    return out


def main() -> int:
    skills_root = Path(tempfile.mkdtemp(prefix="skillmint_smoke_"))
    print(f"# Skillmint end-to-end smoke test")
    print(f"skills_root: {skills_root}\n")

    results = []
    for label, spec in TARGETS.items():
        print(f"--- {label} ---", flush=True)
        r = run_one(label, spec, skills_root)
        results.append(r)
        for k, v in r.items():
            if isinstance(v, list):
                print(f"  {k}:")
                for item in v:
                    print(f"    {item}")
            else:
                print(f"  {k}: {v}")
        print()

    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    fail = 0
    for r in results:
        ok = not any(k.endswith("_error") for k in r)
        status = "OK" if ok else "FAIL"
        if not ok:
            fail += 1
        print(f"  {r['label']:8s} {status}  steps={r.get('step_count')}  sections={r.get('section_count')}  scaffold={r.get('word_count')}w  src={r.get('source_block','')[:50]}")
    print()
    print(f"  {fail} failure(s) of {len(results)}")

    # Cleanup
    shutil.rmtree(skills_root, ignore_errors=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
