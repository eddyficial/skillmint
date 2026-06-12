"""Domain validator stubs with deterministic coverage checks.

These validators do not pretend to execute every domain. They classify likely
domain coverage and enforce that the capability IR carries enough structure for
future domain-specific validators to run safely.
"""
from __future__ import annotations

import re
from typing import Any

from .base import ValidatorContext, validator_result


def _source_blob(context: ValidatorContext) -> str:
    bindings = context.capability.get("sourceBinding", {}).get("sectionBindings", [])
    return " ".join(str(item.get("summary") or "") for item in bindings).lower()


def _matches(blob: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", blob) for word in words)


class KeywordDomainValidator:
    severity = "medium"

    def __init__(
        self,
        validator_id: str,
        category: str,
        keywords: tuple[str, ...],
        check: str,
    ) -> None:
        self.id = validator_id
        self.category = category
        self.keywords = keywords
        self.check = check

    def validate(self, context: ValidatorContext) -> dict[str, Any]:
        blob = _source_blob(context)
        relevant = _matches(blob, self.keywords)
        contract = context.capability.get("contract") or {}
        has_boundary = bool(contract.get("inputs") and contract.get("outputs"))
        if not relevant:
            return validator_result(
                self.id,
                passed=True,
                skipped=True,
                check=self.check,
                evidence="domain keywords not detected",
                severity=self.severity,
                category=self.category,
                score=1.0,
            )
        return validator_result(
            self.id,
            passed=has_boundary,
            skipped=False,
            check=self.check,
            evidence=f"domain detected; typed boundary={has_boundary}",
            severity=self.severity,
            category=self.category,
            score=1.0 if has_boundary else 0.0,
        )


def domain_validators() -> list[Any]:
    return [
        KeywordDomainValidator(
            "code_domain_contract",
            "domain_code",
            ("code", "python", "javascript", "typescript", "api", "function", "test"),
            "Code-like capabilities declare an executable input/output boundary.",
        ),
        KeywordDomainValidator(
            "browser_domain_contract",
            "domain_browser",
            ("browser", "click", "page", "form", "selector", "login"),
            "Browser-like capabilities declare an executable input/output boundary.",
        ),
        KeywordDomainValidator(
            "document_domain_contract",
            "domain_document",
            ("pdf", "document", "docx", "page", "paragraph", "table"),
            "Document-like capabilities declare an executable input/output boundary.",
        ),
        KeywordDomainValidator(
            "api_domain_contract",
            "domain_api",
            ("endpoint", "request", "response", "json", "token", "webhook"),
            "API-like capabilities declare an executable input/output boundary.",
        ),
        KeywordDomainValidator(
            "data_domain_contract",
            "domain_data",
            ("csv", "sql", "spreadsheet", "data", "row", "column"),
            "Data-like capabilities declare an executable input/output boundary.",
        ),
    ]
