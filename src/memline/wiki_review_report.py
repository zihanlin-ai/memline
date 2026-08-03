"""Judge the reviewer's report: is it well-formed, and do its passes agree?

Splitting this out of ``wiki_review`` is not tidiness. The report arrives from
a model, and everything here decides whether to believe its shape -- verdicts
drawn from closed sets, findings that point at citations the bundle actually
contains, omission reviews that name real threads. That decision has to stay
answerable without a network, without an endpoint, and without the code that
obtained the report being able to influence how it is graded.

So this module never calls out and never reads the draft. It takes a report
and the bundle it claims to be about, and returns what is wrong with it.

The merge follows the same discipline. Independent passes disagree, and the
rule is that the strictest verdict wins in both directions -- a claim any pass
called contradicted stays contradicted, an article any pass wanted revised
stays revised. Averaging would let a majority of shallow passes outvote the
one that actually read the evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from memline.wiki_verify import CITATION

CLAIM_VERDICTS = {
    "supported", "partially_supported", "contradicted", "unverifiable",
    "superseded_misused",
}
SCOPE_VERDICTS = {"in_scope", "minor_drift", "major_drift", "unverifiable"}
OVERALL_VERDICTS = {"pass", "revise", "reject"}
CONFIDENCE = {"high", "medium", "low"}
SEVERITIES = {"info", "warning", "error", "critical"}


# The hash that binds a report to the packet it judges. It lives here rather
# than beside the code that stamps it: a report claiming to be about some
# other bundle is exactly the failure this module exists to catch, and the
# check must not depend on the stamping code agreeing about what it computed.
def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_hash(value: dict[str, Any], field: str) -> str:
    copy = dict(value)
    copy.pop(field, None)
    return _sha256_text(_canonical_json(copy))


def _validate_omission_reviews(
    report: dict[str, Any], review_bundle: dict[str, Any], findings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate omission records shared by single-pass and merged reports."""
    known_refs = {item["ref"] for item in review_bundle.get("uncited_evidence") or []}
    for packet in review_bundle.get("claim_packets") or []:
        known_refs.update(c["resolved_ref"] for c in packet["citations"] if c["resolved_ref"])
    article = review_bundle.get("article_markdown") or ""
    omissions = report.get("omission_reviews")
    if not isinstance(omissions, list):
        findings.append({"kind": "omission_reviews_missing"})
        return []
    for index, item in enumerate(omissions):
        if not isinstance(item, dict):
            findings.append({"kind": "omission_review_not_object", "index": index})
            continue
        if item.get("severity") not in SEVERITIES:
            findings.append({"kind": "omission_review_bad_severity", "index": index})
        if not isinstance(item.get("finding"), str) or not item.get("finding", "").strip():
            findings.append({"kind": "omission_review_finding_missing", "index": index})
        refs = item.get("evidence_refs") or []
        if not isinstance(refs, list) or any(ref not in known_refs for ref in refs):
            findings.append({"kind": "omission_review_unknown_evidence", "index": index})
        # A reviewer sees the article and the material it was written from in
        # one document, and the material is *supposed* to be full of ticket
        # numbers and people's names. A sensitivity finding therefore has to
        # quote a string that actually survived into the article.
        quotes = item.get("article_quotes")
        # Only a finding that *asserts* something is in the article has to
        # prove it. A sensitivity note reporting the absence of leaks has
        # nothing to quote, and requiring it invalidated a pass for correctly
        # saying "no sensitive strings appear in article_markdown" — a gate
        # that fires on the right answer teaches its reader to ignore it.
        # `info` is the severity a clearance note carries; anything above it
        # is a claim about published text.
        asserts_a_leak = (item.get("kind") == "sensitivity"
                          and item.get("severity") in ("warning", "error", "critical"))
        if asserts_a_leak and not quotes:
            findings.append({"kind": "sensitivity_finding_without_quotes", "index": index})
        elif quotes is not None:
            if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
                findings.append({"kind": "article_quotes_not_strings", "index": index})
            else:
                for quote in quotes:
                    if quote not in article:
                        findings.append({"kind": "article_quote_not_in_article",
                                         "index": index, "detail": quote[:80]})
    return [item for item in omissions if isinstance(item, dict)]


def validate_review_report(report: dict[str, Any], review_bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate reviewer coverage, hashes, enum values and evidence joins."""
    findings: list[dict[str, Any]] = []
    expected_hash = _content_hash(review_bundle, "review_bundle_sha256")
    if review_bundle.get("review_bundle_sha256") != expected_hash:
        findings.append({"kind": "review_bundle_hash_invalid"})
    for field in ("article_sha256", "review_bundle_sha256"):
        if report.get(field) != review_bundle.get(field):
            findings.append({"kind": "review_hash_mismatch", "field": field})
    if not isinstance(report.get("summary"), str) or not report.get("summary", "").strip():
        findings.append({"kind": "review_summary_missing"})

    expected = {packet["claim_id"]: packet for packet in review_bundle.get("claim_packets") or []}
    reviews = report.get("claim_reviews")
    if not isinstance(reviews, list):
        findings.append({"kind": "claim_reviews_missing"})
        reviews = []
    seen: set[str] = set()
    for item in reviews:
        if not isinstance(item, dict):
            findings.append({"kind": "claim_review_not_object"})
            continue
        claim_id = item.get("claim_id")
        if claim_id in seen:
            findings.append({"kind": "claim_review_duplicate", "claim_id": claim_id})
        seen.add(claim_id)
        packet = expected.get(claim_id)
        if packet is None:
            findings.append({"kind": "claim_review_unknown", "claim_id": claim_id})
            continue
        if item.get("verdict") not in CLAIM_VERDICTS:
            findings.append({"kind": "claim_review_bad_verdict", "claim_id": claim_id})
        if item.get("confidence") not in CONFIDENCE:
            findings.append({"kind": "claim_review_bad_confidence", "claim_id": claim_id})
        if not isinstance(item.get("reason"), str) or not item.get("reason", "").strip():
            findings.append({"kind": "claim_review_reason_missing", "claim_id": claim_id})
        if not isinstance(item.get("suggested_rewrite"), str):
            findings.append({"kind": "claim_review_rewrite_invalid", "claim_id": claim_id})
        allowed = {c["resolved_ref"] for c in packet["citations"] if c["resolved_ref"]}
        used = item.get("evidence_refs") or []
        if not isinstance(used, list) or any(ref not in allowed for ref in used):
            findings.append({"kind": "claim_review_evidence_outside_packet", "claim_id": claim_id})
        elif item.get("verdict") == "supported" and not used:
            findings.append({"kind": "supported_claim_without_evidence", "claim_id": claim_id})
    missing = sorted(set(expected) - seen)
    if missing:
        findings.append({"kind": "claim_reviews_incomplete", "claim_ids": missing,
                         "count": len(missing)})

    scope = report.get("scope_review")
    if not isinstance(scope, dict) or scope.get("verdict") not in SCOPE_VERDICTS:
        findings.append({"kind": "scope_review_invalid"})
    if report.get("overall_verdict") not in OVERALL_VERDICTS:
        findings.append({"kind": "overall_verdict_invalid"})

    omissions = _validate_omission_reviews(report, review_bundle, findings)

    verdicts = [item.get("verdict") for item in reviews if isinstance(item, dict)]
    verdict_counts = dict(sorted(Counter(
        verdict if isinstance(verdict, str) else "<invalid>" for verdict in verdicts
    ).items()))
    severity_counts = dict(sorted(Counter(
        item.get("severity") if isinstance(item.get("severity"), str) else "<invalid>"
        for item in omissions if isinstance(item, dict)
    ).items()))
    semantic_clean = len(verdicts) == len(expected) and all(v == "supported" for v in verdicts)
    omission_clean = all(item.get("severity") == "info"
                         for item in omissions if isinstance(item, dict))
    scope_clean = isinstance(scope, dict) and scope.get("verdict") == "in_scope"
    report_valid = not findings
    return {
        "findings": findings,
        "report_valid": report_valid,
        "deterministic_clean": bool(
            (review_bundle.get("deterministic_report") or {}).get("clean")),
        "semantic_clean": semantic_clean,
        "omission_clean": omission_clean,
        "scope_clean": scope_clean,
        "ready_for_agent_review": report_valid,
        "claims_expected": len(expected),
        "claims_reviewed": len(seen & set(expected)),
        "claim_verdict_counts": verdict_counts,
        "omission_severity_counts": severity_counts,
        "clean": report_valid
        and bool((review_bundle.get("deterministic_report") or {}).get("clean"))
        and semantic_clean and omission_clean and scope_clean
        and report.get("overall_verdict") == "pass",
        "agent_review_required": True,
    }


def validate_merged_review_report(
    report: dict[str, Any], review_bundle: dict[str, Any]
) -> dict[str, Any]:
    """Validate the locally merged shape emitted by :func:`merge_reviews`."""
    findings: list[dict[str, Any]] = []
    expected_hash = _content_hash(review_bundle, "review_bundle_sha256")
    if review_bundle.get("review_bundle_sha256") != expected_hash:
        findings.append({"kind": "review_bundle_hash_invalid"})
    for field in ("article_sha256", "review_bundle_sha256"):
        if report.get(field) != review_bundle.get(field):
            findings.append({"kind": "review_hash_mismatch", "field": field})

    passes = report.get("passes")
    if not isinstance(passes, int) or isinstance(passes, bool) or passes < 1:
        findings.append({"kind": "merged_review_bad_pass_count"})
        passes = 0

    expected = {packet["claim_id"]: packet for packet in review_bundle.get("claim_packets") or []}
    reviews = report.get("claim_reviews")
    if not isinstance(reviews, list):
        findings.append({"kind": "claim_reviews_missing"})
        reviews = []
    seen: set[str] = set()
    for item in reviews:
        if not isinstance(item, dict):
            findings.append({"kind": "claim_review_not_object"})
            continue
        claim_id = item.get("claim_id")
        if claim_id in seen:
            findings.append({"kind": "claim_review_duplicate", "claim_id": claim_id})
        seen.add(claim_id)
        packet = expected.get(claim_id)
        if packet is None:
            findings.append({"kind": "claim_review_unknown", "claim_id": claim_id})
            continue
        verdict = item.get("verdict")
        if verdict not in CLAIM_VERDICTS:
            findings.append({"kind": "claim_review_bad_verdict", "claim_id": claim_id})
        flagged_in = item.get("flagged_in")
        if (not isinstance(flagged_in, int) or isinstance(flagged_in, bool)
                or flagged_in < 0 or flagged_in > passes):
            findings.append({"kind": "merged_claim_bad_flagged_in", "claim_id": claim_id})
        if item.get("of_passes") != passes:
            findings.append({"kind": "merged_claim_bad_pass_total", "claim_id": claim_id})
        merged_findings = item.get("findings")
        if not isinstance(merged_findings, list):
            findings.append({"kind": "merged_claim_findings_missing", "claim_id": claim_id})
            merged_findings = []
        allowed = {c["resolved_ref"] for c in packet["citations"] if c["resolved_ref"]}
        finding_passes: set[int] = set()
        finding_verdicts: list[str] = []
        for merged_finding in merged_findings:
            if not isinstance(merged_finding, dict):
                findings.append({"kind": "merged_claim_finding_not_object",
                                 "claim_id": claim_id})
                continue
            finding_pass = merged_finding.get("pass")
            if (not isinstance(finding_pass, int) or isinstance(finding_pass, bool)
                    or finding_pass < 1 or finding_pass > passes):
                findings.append({"kind": "merged_claim_finding_bad_pass",
                                 "claim_id": claim_id})
            elif finding_pass in finding_passes:
                findings.append({"kind": "merged_claim_finding_duplicate_pass",
                                 "claim_id": claim_id, "pass": finding_pass})
            else:
                finding_passes.add(finding_pass)
            finding_verdict = merged_finding.get("verdict")
            if finding_verdict not in CLAIM_VERDICTS or finding_verdict == "supported":
                findings.append({"kind": "merged_claim_finding_bad_verdict",
                                 "claim_id": claim_id})
            else:
                finding_verdicts.append(finding_verdict)
            if merged_finding.get("confidence") not in CONFIDENCE:
                findings.append({"kind": "claim_review_bad_confidence", "claim_id": claim_id})
            if (not isinstance(merged_finding.get("reason"), str)
                    or not merged_finding.get("reason", "").strip()):
                findings.append({"kind": "claim_review_reason_missing", "claim_id": claim_id})
            if not isinstance(merged_finding.get("suggested_rewrite"), str):
                findings.append({"kind": "claim_review_rewrite_invalid", "claim_id": claim_id})
            used = merged_finding.get("evidence_refs") or []
            if not isinstance(used, list) or any(ref not in allowed for ref in used):
                findings.append({"kind": "claim_review_evidence_outside_packet",
                                 "claim_id": claim_id})
        if isinstance(flagged_in, int) and not isinstance(flagged_in, bool):
            if flagged_in != len(merged_findings):
                findings.append({"kind": "merged_claim_flag_count_mismatch",
                                 "claim_id": claim_id})
        expected_verdict = (max(finding_verdicts, key=lambda value: _VERDICT_RANK[value])
                            if finding_verdicts else "supported")
        if verdict != expected_verdict:
            findings.append({"kind": "merged_claim_verdict_mismatch", "claim_id": claim_id})

    missing = sorted(set(expected) - seen)
    if missing:
        findings.append({"kind": "claim_reviews_incomplete", "claim_ids": missing,
                         "count": len(missing)})

    flagged = [item for item in reviews if isinstance(item, dict)
               and isinstance(item.get("flagged_in"), int) and item.get("flagged_in") > 0]
    if report.get("flagged_claims") != len(flagged):
        findings.append({"kind": "merged_flagged_claim_count_mismatch"})
    unanimous = sum(1 for item in flagged if item.get("flagged_in") == passes)
    if report.get("unanimous_claims") != unanimous:
        findings.append({"kind": "merged_unanimous_claim_count_mismatch"})
    single = sum(1 for item in flagged if item.get("flagged_in") == 1)
    if report.get("single_pass_claims") != single:
        findings.append({"kind": "merged_single_pass_claim_count_mismatch"})

    omissions = _validate_omission_reviews(report, review_bundle, findings)
    aggregate = report.get("validation")
    if not isinstance(aggregate, dict):
        findings.append({"kind": "merged_validation_missing"})
        aggregate = {}
    per_pass = aggregate.get("per_pass")
    if not isinstance(per_pass, list) or len(per_pass) != passes:
        findings.append({"kind": "merged_per_pass_validation_incomplete"})
        per_pass = []
    else:
        pass_numbers = [item.get("pass") for item in per_pass if isinstance(item, dict)]
        if pass_numbers != list(range(1, passes + 1)):
            findings.append({"kind": "merged_per_pass_numbers_invalid"})
    invalid_passes = [item.get("pass") for item in per_pass
                      if isinstance(item, dict) and item.get("report_valid") is not True]
    if aggregate.get("invalid_passes") != invalid_passes:
        findings.append({"kind": "merged_invalid_passes_mismatch"})
    aggregate_valid = bool(per_pass) and not invalid_passes
    if aggregate.get("report_valid") is not aggregate_valid:
        findings.append({"kind": "merged_report_valid_mismatch"})
    if not aggregate_valid:
        findings.append({"kind": "merged_review_contains_invalid_passes",
                         "passes": invalid_passes})
    if aggregate.get("claims_expected") != len(expected):
        findings.append({"kind": "merged_claims_expected_mismatch"})
    if aggregate.get("deterministic_clean") is not bool(
            (review_bundle.get("deterministic_report") or {}).get("clean")):
        findings.append({"kind": "merged_deterministic_status_mismatch"})
    scope_clean = aggregate.get("scope_clean") is True
    if report.get("overall_verdict") not in OVERALL_VERDICTS:
        findings.append({"kind": "overall_verdict_invalid"})

    provenance = report.get("review_provenance")
    if not isinstance(provenance, list) or len(provenance) != passes:
        findings.append({"kind": "merged_review_provenance_incomplete"})

    verdicts = [item.get("verdict") for item in reviews if isinstance(item, dict)]
    semantic_clean = len(verdicts) == len(expected) and all(v == "supported" for v in verdicts)
    omission_clean = all(item.get("severity") == "info" for item in omissions)
    deterministic_clean = bool((review_bundle.get("deterministic_report") or {}).get("clean"))
    verdict_counts = Counter(
        verdict if isinstance(verdict, str) else "<invalid>" for verdict in verdicts)
    severity_counts = Counter(
        item.get("severity") if isinstance(item.get("severity"), str) else "<invalid>"
        for item in omissions)
    report_valid = not findings
    return {
        "findings": findings,
        "report_valid": report_valid,
        "deterministic_clean": deterministic_clean,
        "semantic_clean": semantic_clean,
        "omission_clean": omission_clean,
        "scope_clean": scope_clean,
        "claims_expected": len(expected),
        "claims_reviewed": len(seen & set(expected)),
        "claim_verdict_counts": dict(sorted(verdict_counts.items())),
        "omission_severity_counts": dict(sorted(severity_counts.items())),
        "clean": report_valid and deterministic_clean and semantic_clean
        and omission_clean and scope_clean and report.get("overall_verdict") == "pass",
        "agent_review_required": True,
    }


def validate_review_artifact(
    report: dict[str, Any], review_bundle: dict[str, Any]
) -> dict[str, Any]:
    """Validate either a raw single pass or the local multi-pass merge."""
    if "passes" in report:
        return validate_merged_review_report(report, review_bundle)
    return validate_review_report(report, review_bundle)

# Strictest wins when passes disagree, in both directions a verdict can go.
_VERDICT_RANK = {"supported": 0, "unverifiable": 1, "partially_supported": 2,
                 "superseded_misused": 3, "contradicted": 4}
_OVERALL_RANK = {"pass": 0, "revise": 1, "reject": 2}


def merge_reviews(reports: list[dict[str, Any]], prior: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
    """Fold several independent passes over one draft into a union.

    One pass is not a measurement. Five audits of one unchanged article flagged
    17, 1, 1, 5 and 19 claims — and the last two ran on the *same* prompt as
    each other. The union across three of them is 21, which no single pass
    reached. Passes also surface claims no earlier pass did, so this is not one
    thorough reading plus four lazy ones; each reading stops somewhere
    different.

    So the merge takes the union, not the majority. A claim flagged once is
    flagged, and ``flagged_in`` records how many passes saw it — the number a
    reviewer should read as strength of signal, since a finding every pass
    agrees on is a different thing from one that surfaced once. Voting would
    discard precisely the findings this exists to buy: the ones easy to miss.
    With this much spread, a majority rule over three passes would have thrown
    away most of what was found.

    The reverse never applies. A pass cannot clear a claim another flagged,
    because "I did not notice this" and "this is fine" are not the same
    statement, and nothing in the report distinguishes them.

    What the spread does *not* license is reading a low ``flagged_in`` as a
    weak finding. The one claim flagged by every pass was also the only one any
    pass marked high-confidence; everything else, including a genuine causal
    error confirmed by hand against the evidence, moved in and out of view
    between runs.
    """
    if not reports:
        raise ValueError("no review passes to merge")
    # Auditing the same article again adds passes to the union rather than
    # re-rolling it. Two audits of one unchanged draft returned 5 findings and
    # 19; replacing the first with the second would have discarded fourteen
    # real ones and looked like an update. The article's hash is what makes
    # this safe, and the caller checks it before passing a prior report.
    claims: dict[str, dict[str, Any]] = {}
    offset = 0
    if prior:
        offset = int(prior.get("passes") or 0)
        for item in prior.get("claim_reviews") or []:
            claims[item["claim_id"]] = {**item, "findings": list(item.get("findings") or [])}
    total = offset + len(reports)
    article_sha256 = next((r.get("article_sha256") for r in reports if r.get("article_sha256")),
                          (prior or {}).get("article_sha256"))
    review_bundle_sha256 = next(
        (r.get("review_bundle_sha256") for r in reports if r.get("review_bundle_sha256")),
        (prior or {}).get("review_bundle_sha256"),
    )
    for entry in claims.values():
        entry["of_passes"] = total

    for index, report in enumerate(reports, start=offset):
        for item in report.get("claim_reviews") or []:
            if not isinstance(item, dict) or not item.get("claim_id"):
                continue
            claim_id = item["claim_id"]
            entry = claims.setdefault(claim_id, {
                "claim_id": claim_id, "verdict": "supported", "flagged_in": 0,
                "of_passes": total, "findings": [],
            })
            verdict = item.get("verdict")
            if _VERDICT_RANK.get(verdict, 0) > _VERDICT_RANK.get(entry["verdict"], 0):
                entry["verdict"] = verdict
            if verdict != "supported":
                entry["flagged_in"] += 1
                entry["findings"].append({
                    "pass": index + 1, "verdict": verdict,
                    "confidence": item.get("confidence"), "reason": item.get("reason"),
                    "suggested_rewrite": item.get("suggested_rewrite"),
                    "evidence_refs": item.get("evidence_refs"),
                })

    omissions = list((prior or {}).get("omission_reviews") or [])
    omissions += [{**item, "pass": index + 1}
                  for index, report in enumerate(reports, start=offset)
                  for item in report.get("omission_reviews") or []
                  if isinstance(item, dict)]

    overall = max([r.get("overall_verdict") for r in reports]
                  + ([(prior or {}).get("overall_verdict")] if prior else []),
                  key=lambda v: _OVERALL_RANK.get(v, 0), default=None)
    validations = [r.get("validation") or {} for r in reports]
    prior_passes = list(((prior or {}).get("validation") or {}).get("per_pass") or [])
    flagged = [c for c in claims.values() if c["flagged_in"]]
    return {
        "passes": total,
        # The article these passes read. Accumulating onto a different one
        # would silently pool findings about two different texts.
        "article_sha256": article_sha256,
        "review_bundle_sha256": review_bundle_sha256,
        "overall_verdict": overall,
        "claim_reviews": sorted(claims.values(), key=lambda c: c["claim_id"]),
        "omission_reviews": omissions,
        "flagged_claims": len(flagged),
        # How much a second opinion actually bought. All passes agreeing is a
        # reason to trust a finding; only one pass seeing it is a reason to run
        # more passes, not to discount it.
        "unanimous_claims": sum(1 for c in flagged if c["flagged_in"] == total),
        "single_pass_claims": sum(1 for c in flagged if c["flagged_in"] == 1),
        "validation": {
            # True only when every pass honoured the contract. A pass that
            # broke it still contributes its findings — the union does not
            # discard what an otherwise-sloppy reader saw — so the scope of the
            # breach is named rather than left to colour the whole report.
            "report_valid": all(v.get("report_valid") for v in validations)
            and all(p.get("report_valid") for p in prior_passes),
            "invalid_passes": [p["pass"] for p in prior_passes if not p.get("report_valid")]
            + [i + 1 for i, v in enumerate(validations, start=offset)
               if not v.get("report_valid")],
            "deterministic_clean": all(v.get("deterministic_clean") for v in validations),
            "scope_clean": all(v.get("scope_clean") for v in validations),
            "claims_expected": max((v.get("claims_expected") or 0 for v in validations), default=0),
            "per_pass": prior_passes + [
                {"pass": i + 1, "report_valid": v.get("report_valid"),
                 "findings": v.get("findings") or [],
                 "claim_verdict_counts": v.get("claim_verdict_counts")}
                for i, v in enumerate(validations, start=offset)],
            "agent_review_required": True,
        },
        "review_provenance": list((prior or {}).get("review_provenance") or [])
        + [r.get("review_provenance") for r in reports],
    }
