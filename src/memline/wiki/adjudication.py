"""Bind an agent's materiality decisions to one immutable review report.

The external reviewer discovers possible defects.  It does not own the
publication gate: an agent decides whether each finding could make a reader
believe an invalid result, misunderstand the current state, or make a
different engineering decision.  This module makes that hand-off durable and
machine-checkable without asking another model.
"""

from __future__ import annotations

from typing import Any

from memline.wiki.artifacts import artifact_sha256

SCHEMA_VERSION = 1
DECISIONS = {"blocking", "non_blocking"}
BLOCKING_CATEGORIES = {
    "factual_contradiction",
    "superseded_current_claim",
    "core_claim_unverifiable",
    "decision_changing_omission",
    "severe_causal_overclaim",
    "safety_privacy_or_scope",
}
NON_BLOCKING_CATEGORIES = {"non_material"}
MATERIALITY_TEST = (
    "If the reader did not know this finding, could they believe an invalid "
    "result, misunderstand the current state, or make a different engineering decision?"
)


def review_report_sha256(report: dict[str, Any]) -> str:
    """Hash the complete saved report, including provenance and validation."""
    return artifact_sha256(report)


def collect_review_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every substantive claim, omission, and scope finding with a stable id."""
    findings: list[dict[str, Any]] = []
    merged = "passes" in report

    for claim in report.get("claim_reviews") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        if merged:
            for item in claim.get("findings") or []:
                if not isinstance(item, dict):
                    continue
                pass_number = item.get("pass")
                findings.append({
                    "finding_id": f"claim:{claim_id}:pass:{pass_number}",
                    "source": "claim",
                    "pass": pass_number,
                    "claim_id": claim_id,
                    "reviewer_verdict": item.get("verdict"),
                    "confidence": item.get("confidence"),
                    "finding": item.get("reason"),
                    "evidence_refs": item.get("evidence_refs") or [],
                    "suggested_rewrite": item.get("suggested_rewrite") or "",
                })
        elif claim.get("verdict") != "supported":
            findings.append({
                "finding_id": f"claim:{claim_id}",
                "source": "claim",
                "claim_id": claim_id,
                "reviewer_verdict": claim.get("verdict"),
                "confidence": claim.get("confidence"),
                "finding": claim.get("reason"),
                "evidence_refs": claim.get("evidence_refs") or [],
                "suggested_rewrite": claim.get("suggested_rewrite") or "",
            })

    for index, item in enumerate(report.get("omission_reviews") or [], start=1):
        if not isinstance(item, dict):
            continue
        pass_number = item.get("pass") if merged else None
        finding_id = (f"omission:pass:{pass_number}:item:{index}" if merged
                      else f"omission:item:{index}")
        findings.append({
            "finding_id": finding_id,
            "source": "omission",
            **({"pass": pass_number} if merged else {}),
            "reviewer_severity": item.get("severity"),
            "reviewer_kind": item.get("kind"),
            "finding": item.get("finding"),
            "evidence_refs": item.get("evidence_refs") or [],
            "article_quotes": item.get("article_quotes") or [],
            "article_location": item.get("article_location") or "",
        })

    if merged:
        scope_reviews = report.get("scope_reviews")
        if isinstance(scope_reviews, list):
            for item in scope_reviews:
                if not isinstance(item, dict) or item.get("verdict") == "in_scope":
                    continue
                pass_number = item.get("pass")
                findings.append({
                    "finding_id": f"scope:pass:{pass_number}",
                    "source": "scope",
                    "pass": pass_number,
                    "reviewer_verdict": item.get("verdict"),
                    "finding": item.get("reason"),
                    "evidence_refs": [],
                })
        elif (report.get("validation") or {}).get("scope_clean") is not True:
            # Reports created before scope details were retained remain
            # adjudicable instead of becoming permanently unpublishable.
            findings.append({
                "finding_id": "scope:legacy-aggregate",
                "source": "scope",
                "reviewer_verdict": "legacy_non_clean",
                "finding": "A legacy merged review recorded a non-clean scope result.",
                "evidence_refs": [],
            })
    else:
        scope = report.get("scope_review")
        if isinstance(scope, dict) and scope.get("verdict") != "in_scope":
            findings.append({
                "finding_id": "scope",
                "source": "scope",
                "reviewer_verdict": scope.get("verdict"),
                "finding": scope.get("reason"),
                "evidence_refs": [],
            })
    return findings


def build_adjudication(report: dict[str, Any]) -> dict[str, Any]:
    """Create a pending adjudication template for one exact review report."""
    return {
        "schema_version": SCHEMA_VERSION,
        "article_sha256": report.get("article_sha256"),
        "review_bundle_sha256": report.get("review_bundle_sha256"),
        "review_report_sha256": review_report_sha256(report),
        "materiality_test": MATERIALITY_TEST,
        "allowed_categories": {
            "blocking": sorted(BLOCKING_CATEGORIES),
            "non_blocking": sorted(NON_BLOCKING_CATEGORIES),
        },
        "findings": [
            {**finding, "decision": None, "category": None, "reason": ""}
            for finding in collect_review_findings(report)
        ],
    }


def validate_adjudication(
    adjudication: dict[str, Any] | None, report: dict[str, Any]
) -> dict[str, Any]:
    """Validate complete Agent adjudication and count unresolved blockers."""
    expected = {item["finding_id"]: item for item in collect_review_findings(report)}
    errors: list[dict[str, Any]] = []
    if adjudication is None:
        if expected:
            errors.append({"kind": "adjudication_missing"})
        return {
            "findings": errors,
            "valid": not errors,
            "required": bool(expected),
            "total_findings": len(expected),
            "adjudicated_findings": 0,
            "unresolved_blocking_findings": 0,
            "non_blocking_findings": 0,
            "clean": not errors,
        }

    if adjudication.get("schema_version") != SCHEMA_VERSION:
        errors.append({"kind": "adjudication_schema_invalid"})
    for field in ("article_sha256", "review_bundle_sha256"):
        if adjudication.get(field) != report.get(field):
            errors.append({"kind": "adjudication_hash_mismatch", "field": field})
    if adjudication.get("review_report_sha256") != review_report_sha256(report):
        errors.append({"kind": "adjudication_hash_mismatch", "field": "review_report_sha256"})

    items = adjudication.get("findings")
    if not isinstance(items, list):
        errors.append({"kind": "adjudication_findings_missing"})
        items = []
    seen: set[str] = set()
    blocking = non_blocking = adjudicated = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"kind": "adjudication_finding_not_object", "index": index})
            continue
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str):
            errors.append({"kind": "adjudication_finding_id_invalid", "index": index})
            continue
        if finding_id in seen:
            errors.append({"kind": "adjudication_finding_duplicate", "finding_id": finding_id})
        seen.add(finding_id)
        if finding_id not in expected:
            errors.append({"kind": "adjudication_finding_unknown", "finding_id": finding_id})
            continue
        decision = item.get("decision")
        category = item.get("category")
        reason = item.get("reason")
        if decision not in DECISIONS:
            errors.append({"kind": "adjudication_decision_invalid", "finding_id": finding_id})
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append({"kind": "adjudication_reason_missing", "finding_id": finding_id})
            continue
        allowed_categories = (BLOCKING_CATEGORIES if decision == "blocking"
                              else NON_BLOCKING_CATEGORIES)
        if category not in allowed_categories:
            errors.append({"kind": "adjudication_category_invalid", "finding_id": finding_id,
                           "decision": decision})
            continue
        adjudicated += 1
        if decision == "blocking":
            blocking += 1
        else:
            non_blocking += 1

    missing = sorted(set(expected) - seen)
    if missing:
        errors.append({"kind": "adjudication_findings_incomplete", "finding_ids": missing,
                       "count": len(missing)})
    valid = not errors
    return {
        "findings": errors,
        "valid": valid,
        "required": bool(expected),
        "total_findings": len(expected),
        "adjudicated_findings": adjudicated,
        "unresolved_blocking_findings": blocking,
        "non_blocking_findings": non_blocking,
        "clean": valid and blocking == 0,
    }
