"""The wiki pipeline commands. Logic lives in the wiki_* modules; these parse and print."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import click
import typer

from memline.cli import _support

@_support.wiki_app.command("close-run")
def wiki_close_run(
    state: Path = typer.Argument(..., help="state/compile.json."),
    started_at: Optional[str] = typer.Option(
        None, "--started-at",
        help="When the run began READING. Pass the value the plan recorded; omitting it "
             "stamps now, which is only correct if nothing was written during the run."),
    source_dir: Optional[Path] = typer.Option(None, "--source-dir", help="sources/, to hash."),
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u"),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Advance the compile cursor. Only for a run that actually completed."""
    from memline.wiki_state import close_run

    memories, read_at = _support.read_all_memories(user_id)
    new = close_run(state, started_at=started_at or read_at, memories=memories,
                    source_dir=source_dir)
    _support.output({**new, "boundary_memory_ids": len(new["boundary_memory_ids"]),
            "source_hashes": len(new["source_hashes"]), "memories_read": len(memories)},
           command="wiki-close-run", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("plan")
def wiki_plan(
    out: Optional[Path] = typer.Option(None, "--out", help="Write the batch plan here."),
    since: Optional[str] = typer.Option(
        None, "--since",
        help="Incremental run: plan only what moved at or after this timestamp. "
             "A session that gained a memory is replanned WHOLE, since a profile "
             "describes the session and not the increment."),
    max_memories: int = typer.Option(275, "--max-memories", help="Ceiling for one batch."),
    pack_threshold: int = typer.Option(
        60, "--pack-threshold", help="At or above this size a session travels alone."
    ),
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u", help="Filter by user."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Plan how the store is cut into batches for wiki topic profiling."""
    from memline.wiki_batch import plan_batches, plan_summary

    memories, read_at = _support.read_all_memories(user_id)
    batches = plan_batches(memories, since=since, max_memories=max_memories,
                           pack_threshold=pack_threshold)
    if out:
        out.write_text(json.dumps(batches, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {**plan_summary(batches), "plan_path": str(out) if out else None,
               "memories_read": len(memories),
               # Hand this to `wiki close-run`: the cursor must record when the
               # run began reading, and this is that moment.
               "read_at": read_at}
    _support.output(summary if out else {"summary": summary, "batches": batches},
           command="wiki-batch", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("profile")
def wiki_profile(
    plan: Optional[Path] = typer.Argument(None, help="Batch plan from wiki-batch."),
    prompt: Optional[Path] = typer.Option(
        None, "--prompt", help="Override the packaged prompt template."),
    out_dir: Path = typer.Option(..., "--out-dir", help="Directory for raw profiles, one file per batch."),
    kinds: str = typer.Option("session,pack,session-part", "--kinds",
                              help="Batch kinds to profile. Ledger chunks are handled by local agents."),
    concurrency: int = typer.Option(2, "--concurrency", help="Parallel calls; keep low, the relay queues."),
    max_tokens: int = typer.Option(128000, "--max-tokens",
        help="One purse for reasoning AND output. A long think starves the answer, and truncation is a wasted call, not a cheaper one."),
    source_dir: Optional[Path] = typer.Option(
        None, "--source-dir", help="Profile these Markdown files instead of memory batches."
    ),
    user_id: str = typer.Option(_support.DEFAULT_USER_ID, "--user-id", "-u"),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Profile batches (or source documents) into raw per-batch topic profiles."""
    from memline.wiki_profile import default_prompt, profile_batches, profile_sources

    template = (prompt.read_text(encoding="utf-8") if prompt
                else default_prompt("wiki-profile-source" if source_dir else "wiki-profile-session"))
    if source_dir:
        summary = profile_sources(source_dir, template, out_dir, max_tokens=max_tokens,
                                  concurrency=concurrency, log=lambda m: _support.console.print(m))
    else:
        batches = json.loads(plan.read_text(encoding="utf-8"))
        wanted = [mid for b in batches if b["kind"] in tuple(kinds.split(","))
                  for mid in b["memory_ids"]]
        texts = {row['id']: row for row in _support.read_all_memories(user_id)[0]}
        missing = [mid for mid in wanted if mid not in texts]
        if missing:
            _support.console.print(f"[yellow]{len(missing)} planned memories no longer in the store[/yellow]")
        summary = profile_batches(batches, texts, template, out_dir,
                                  kinds=tuple(kinds.split(",")), concurrency=concurrency,
                                  max_tokens=max_tokens, log=lambda m: _support.console.print(m))
    _support.output(summary, command="wiki-profile", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("bundle")
def wiki_bundle(
    memory_ids: list[str] = typer.Argument(None, help="Refs to bundle: memory ids or sources/<path>#<heading>."),
    ids_file: Optional[Path] = typer.Option(
        None, "--ids-file", help="File with one ref per line (added to any arguments)."
    ),
    wiki_root: Optional[Path] = typer.Option(
        None, "--wiki-root", help="Wiki root, required to resolve sources/ refs."),
    out: Optional[Path] = typer.Option(None, "--out", help="Write the bundle here (default: stdout)."),
    mapping_out: Optional[Path] = typer.Option(
        None, "--mapping-out", help="Write the placeholder->original mapping here. Keep it local."
    ),
    no_sanitize: bool = typer.Option(
        False, "--no-sanitize", help="Skip placeholder substitution. Never for an outbound call."
    ),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Resolve memories into a sanitized bundle for a call to an external model."""
    from memline.bundle import build_bundle

    ids = list(memory_ids or [])
    if ids_file:
        ids += [line.strip() for line in ids_file.read_text().splitlines() if line.strip()]
    if not ids:
        raise typer.BadParameter("no memory ids given")
    bundle, mapping = build_bundle(ids, _support.execute, sanitize=not no_sanitize, wiki_root=wiki_root)
    if out:
        out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    if mapping_out:
        mapping_out.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "memory_count": bundle["memory_count"],
        "source_section_count": bundle["source_section_count"],
        "unresolved": len(bundle["unresolved"]),
        "sanitized": bundle["sanitized"],
        "placeholder_counts": bundle["sanitization"]["placeholder_counts"],
        "review_flags": len(bundle["sanitization"]["review_flags"]),
        "bundle_path": str(out) if out else None,
    }
    _support.output(summary if out else bundle, command="bundle", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("suggest")
def wiki_suggest(
    associations: Path = typer.Argument(..., help="Association decision from the agent."),
    profile_dir: list[Path] = typer.Option(..., "--profiles",
        help="Directory of raw profiles. Repeat for memory batches and source documents."),
    out: Path = typer.Option(..., "--out", help="Write suggestions.jsonl here."),
    run: int = typer.Option(1, "--run", help="Run number, used for suggestion ids."),
    ledger: Optional[Path] = typer.Option(
        None, "--ledger", help="decisions.md, so rejected topics are not proposed again."
    ),
    wiki_root: Optional[Path] = typer.Option(
        None, "--wiki-root",
        help="Wiki root whose published pages are checked for retired evidence. "
             "Defaults to the parent of --out."),
    skip_page_check: bool = typer.Option(
        False, "--skip-page-check",
        help="Do not check published pages. A deleted memory is invisible to the "
             "incremental plan, so skipping this hides it permanently."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Assemble reviewed associations into the suggestion list, resolving evidence."""
    from memline.wiki_check import run_check
    from memline.wiki_suggest import build_suggestions, load_threads, maintenance_suggestions

    def resolve(memory_id: str) -> str | None:
        try:
            record = _support.execute("get", {"memory_id": memory_id})
        except Exception:  # noqa: BLE001 - an id that will not resolve is reported, not raised
            return None
        return record.get("memory") if isinstance(record, dict) else None

    topics = json.loads(associations.read_text(encoding="utf-8"))
    topics = topics.get("topics", topics) if isinstance(topics, dict) else topics
    suggestions, report = build_suggestions(
        topics, load_threads(*profile_dir), resolve, run=run, ledger=ledger)

    # The published pages are the only place a *deleted* memory is still
    # recorded: the incremental plan iterates what exists, so a citation whose
    # memory is gone leaves nothing for it to notice. Running the check here
    # rather than asking whoever drives compile to remember it is the whole
    # point — the instruction existed for weeks, named a command that had since
    # been renamed, and produced not one maintenance suggestion.
    root = wiki_root or out.resolve().parent.parent
    page_check: dict[str, Any] = {"skipped": True}
    if not skip_page_check and (root / "content").is_dir():
        page_check = run_check(root, _support.execute)
        maintenance = maintenance_suggestions(page_check, run=run,
                                              start_number=len(suggestions))
        if maintenance:
            _support.console.print(f"[yellow]{len(maintenance)} published page(s) need attention: "
                          f"{page_check['flag_count']} flag(s)[/yellow]")
        suggestions += maintenance

    out.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in suggestions),
                   encoding="utf-8")
    _support.output({**report, "suggestions": len(suggestions),
            "by_type": _support._count_types(suggestions),
            "page_check": {k: v for k, v in page_check.items() if k != "flags"},
            "out": str(out)},
           command="wiki-suggest", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("draft")
def wiki_draft(
    topics: Path = typer.Argument(..., help="accepted-topics.jsonl."),
    out_dir: Path = typer.Option(..., "--out-dir", help="Where drafts and their bundles go."),
    wiki_root: Path = typer.Option(Path("."), "--wiki-root", help="Wiki root, for sources/."),
    only: Optional[str] = typer.Option(None, "--only", help="Draft just this topic_key or id."),
    review_file: Optional[Path] = typer.Option(
        None, "--review-file",
        help="Rulings on sensitive-looking values: {\"redact\": {value: category}, \"cleared\": [...]}. "
             "An unruled personal name or address blocks the call."),
    prompt: Optional[Path] = typer.Option(None, "--prompt", help="Override the packaged prompt."),
    # One purse for reasoning AND output. The first drafting round ran at 64000
    # and read its own truncations as a vendor ceiling; they were this number.
    max_tokens: int = typer.Option(128000, "--max-tokens",
        help="Budget for reasoning AND output together. Truncation is a wasted call, not a cheaper one."),
    force: bool = typer.Option(False, "--force",
        help="Redraft topics that already have a draft, overwriting it."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Draft accepted topics from their evidence on the configured drafting endpoint."""
    from memline.wiki_draft import draft_topic
    from memline.wiki_profile import default_prompt

    template = prompt.read_text(encoding="utf-8") if prompt else default_prompt("wiki-draft")
    queue = [json.loads(line) for line in topics.read_text(encoding="utf-8").splitlines() if line.strip()]
    if only:
        queue = [t for t in queue if only in (t.get("topic_key"), t.get("id"))]
    if not queue:
        raise typer.BadParameter("no topics selected")
    done, failed = [], []
    for topic in queue:
        # Drafting is expensive and a draft may have been edited by hand since,
        # so an existing one is kept unless overwriting it is asked for by name.
        if not force and (out_dir / f"{topic.get('topic_key') or topic['id']}.md").exists():
            _support.console.print(f"{topic.get('topic_key')}: already drafted, skipping (--force to redraft)")
            continue
        try:
            done.append(draft_topic(topic, _support.execute, template, out_dir, wiki_root=wiki_root,
                                    review_file=review_file, max_tokens=max_tokens,
                                    log=lambda m: _support.console.print(m)))
        except Exception as exc:  # noqa: BLE001 - one bad topic must not stop the queue
            _support.console.print(f"[red]{topic.get('topic_key')}: {exc}[/red]")
            failed.append({"topic_key": topic.get("topic_key"), "error": str(exc)})
    _support.output({"drafted": done, "failed": failed, "out_dir": str(out_dir)},
           command="wiki-draft", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("check-draft")
def wiki_check_draft(
    draft: Path = typer.Argument(..., help="Draft Markdown written by wiki-draft."),
    review: Optional[Path] = typer.Option(
        None, "--review", help="Review report JSON to bind and validate against this draft."),
    review_bundle: Optional[Path] = typer.Option(
        None, "--review-bundle", help="Review bundle used by --review (defaults beside draft)."),
    sensitivity_review: Optional[Path] = typer.Option(
        None, "--sensitivity-review",
        help="Human redaction rulings; auto-detected for workspace drafts."),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero unless every active gate is clean."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Check a draft and, optionally, bind an external review to its exact hashes."""
    from memline.wiki_verify import verify

    stem = draft.with_suffix("")
    bundle_path = Path(str(stem) + ".bundle.json")
    claims_path = Path(str(stem) + ".claims.json")
    if not bundle_path.is_file():
        raise typer.BadParameter(f"no bundle beside the draft: {bundle_path}")
    draft_text = draft.read_text(encoding="utf-8")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    claims = (json.loads(claims_path.read_text(encoding="utf-8"))
              if claims_path.is_file() else None)
    report = verify(draft_text, bundle, claims)
    result: dict[str, Any] = {"draft": str(draft), **report}
    gate_clean = report["clean"]
    if review:
        from memline.wiki_review import build_review_bundle
        from memline.wiki_review_report import validate_review_artifact

        review_bundle_path = review_bundle or Path(str(stem) + ".review-bundle.json")
        if not review_bundle_path.is_file():
            raise typer.BadParameter(f"no review bundle: {review_bundle_path}")
        if not review.is_file():
            raise typer.BadParameter(f"no review report: {review}")
        compiled = json.loads(review_bundle_path.read_text(encoding="utf-8"))
        # Rebuilding makes a stale article, evidence bundle or claims sidecar
        # visible even when the old artifacts still agree with one another.
        _, _, _, current_topic = _support._review_artifacts(draft)
        review_evidence, review_claims, review_topic = _support._sanitize_review_artifacts(
            draft, draft_text, bundle, claims,
            current_topic if current_topic is not None else compiled.get("approved_topic"),
            sensitivity_review)
        rebuilt = build_review_bundle(
            draft_text, review_evidence, review_claims, review_topic, draft_name=draft.name)
        if rebuilt["review_bundle_sha256"] != compiled.get("review_bundle_sha256"):
            result["review_validation"] = {
                "clean": False,
                "report_valid": False,
                "findings": [{"kind": "review_bundle_stale"}],
                "agent_review_required": True,
            }
        else:
            review_report = json.loads(review.read_text(encoding="utf-8"))
            result["review_validation"] = validate_review_artifact(
                review_report, compiled)
        gate_clean = bool(result["review_validation"].get("clean"))
    _support.output(result, command="wiki-verify",
           fmt=_support.chosen_format(output_format, json_flag))
    if strict and not gate_clean:
        raise typer.Exit(code=1)


@_support.wiki_app.command("prepare-review")
def wiki_prepare_review(
    draft: Path = typer.Argument(..., help="Draft Markdown written by wiki-draft."),
    out: Optional[Path] = typer.Option(None, "--out", help="Review bundle JSON path."),
    topics: Optional[Path] = typer.Option(
        None, "--topics", help="Accepted topics JSONL; auto-detected for workspace drafts."),
    sensitivity_review: Optional[Path] = typer.Option(
        None, "--sensitivity-review",
        help="Human redaction rulings; auto-detected for workspace drafts."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Resolve every citation and attach its evidence in an immutable review bundle."""
    from memline.wiki_review import build_review_bundle

    draft_text, bundle, claims, topic = _support._review_artifacts(draft, topics)
    bundle, claims, topic = _support._sanitize_review_artifacts(
        draft, draft_text, bundle, claims, topic, sensitivity_review)
    review_bundle = build_review_bundle(
        draft_text, bundle, claims, topic, draft_name=draft.name)
    target = out or Path(str(draft.with_suffix("")) + ".review-bundle.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(review_bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    unresolved = sum(
        citation["status"] in {"missing", "ambiguous"}
        for packet in review_bundle["claim_packets"] for citation in packet["citations"])
    _support.output({
        "draft": str(draft), "out": str(target),
        "review_bundle_sha256": review_bundle["review_bundle_sha256"],
        "claim_packets": len(review_bundle["claim_packets"]),
        "uncited_passages": len(review_bundle["uncited_passages"]),
        "uncited_evidence": len(review_bundle["uncited_evidence"]),
        "unresolved_citations": unresolved,
        "deterministic_clean": review_bundle["deterministic_report"]["clean"],
    }, command="wiki-prepare-review", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("review-draft")
def wiki_review_draft(
    draft: Path = typer.Argument(..., help="Draft Markdown written by wiki-draft."),
    out: Optional[Path] = typer.Option(None, "--out", help="Review report JSON path."),
    review_bundle_out: Optional[Path] = typer.Option(
        None, "--review-bundle-out", help="Compiled review bundle JSON path."),
    topics: Optional[Path] = typer.Option(
        None, "--topics", help="Accepted topics JSONL; auto-detected for workspace drafts."),
    sensitivity_review: Optional[Path] = typer.Option(
        None, "--sensitivity-review",
        help="Human redaction rulings; auto-detected for workspace drafts."),
    prompt: Optional[Path] = typer.Option(None, "--prompt", help="Override the review prompt."),
    passes: int = typer.Option(3, "--passes",
        help="Independent audits to merge. One pass misses findings it does not contradict."),
    fresh: bool = typer.Option(False, "--fresh",
        help="Discard the existing review instead of adding these passes to it."),
    max_tokens: int = typer.Option(64000, "--max-tokens"),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Compile the evidence packet, audit it on the configured review endpoint, and validate the report."""
    from memline.wiki_profile import default_prompt
    from memline.wiki_review import build_review_bundle, load_prior_review, run_review_passes

    draft_text, bundle, claims, topic = _support._review_artifacts(draft, topics)
    if bundle.get("sanitized") is not True:
        raise typer.BadParameter("the evidence bundle is not marked sanitized; refusing external review")
    bundle, claims, topic = _support._sanitize_review_artifacts(
        draft, draft_text, bundle, claims, topic, sensitivity_review)

    compiled = build_review_bundle(draft_text, bundle, claims, topic, draft_name=draft.name)
    bundle_target = review_bundle_out or Path(str(draft.with_suffix("")) + ".review-bundle.json")
    bundle_target.parent.mkdir(parents=True, exist_ok=True)
    bundle_target.write_text(json.dumps(compiled, ensure_ascii=False, indent=1), encoding="utf-8")
    template = prompt.read_text(encoding="utf-8") if prompt else default_prompt("wiki-review")
    target = out or Path(str(draft.with_suffix("")) + ".review.json")
    # Auditing the same text again buys coverage rather than a re-roll: one
    # unchanged article returned 5 findings on one pass and 19 on another, so
    # replacing the old report would discard real findings and look like an
    # update. A changed article makes the old report describe sentences that no
    # longer exist, and the hash catches that on its own.
    prior = None if fresh else load_prior_review(
        target, compiled["article_sha256"], compiled["review_bundle_sha256"])
    if prior:
        _support.console.print(f"adding {passes} pass(es) to the existing {prior['passes']} "
                      f"for this unchanged article")
    report = run_review_passes(compiled, template, passes=passes, max_tokens=max_tokens,
                               prior=prior, log=lambda m: _support.console.print(m))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    validation = report["validation"]
    _support.output({
        "draft": str(draft), "review_bundle": str(bundle_target), "out": str(target),
        "review_bundle_sha256": compiled["review_bundle_sha256"],
        "passes": report["passes"],
        "overall_verdict": report.get("overall_verdict"),
        "claim_reviews": len(report.get("claim_reviews") or []),
        "flagged_claims": report["flagged_claims"],
        "unanimous_claims": report["unanimous_claims"],
        "single_pass_claims": report["single_pass_claims"],
        "omission_reviews": len(report.get("omission_reviews") or []),
        "report_valid": validation["report_valid"],
        "per_pass": validation["per_pass"],
        "agent_review_required": True,
        "provenance": report.get("review_provenance"),
    }, command="wiki-review-draft", fmt=_support.chosen_format(output_format, json_flag))


@_support.wiki_app.command("check-pages")
def wiki_check_pages(
    wiki_root: Optional[Path] = typer.Argument(
        None, help="Wiki root directory (contains content/). Default: <workspace>/.agent-memory/wiki."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero when any page or metadata gate fails."
    ),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Check wiki provenance and internal links against current memory/source state (read-only)."""
    from memline.wiki_check import run_check

    root = wiki_root or (_support.ROOT / ".agent-memory" / "wiki")
    report = run_check(root, _support.execute)
    _support.output(report, command="wiki-check", fmt=_support.chosen_format(output_format, json_flag))
    if strict and not report["clean"]:
        raise typer.Exit(code=1)


@_support.wiki_app.command("nav")
def wiki_nav(
    wiki_root: Optional[Path] = typer.Argument(
        None, help="Wiki root (contains content/). Default: <workspace>/.agent-memory/wiki."),
    check: bool = typer.Option(
        True, "--check/--no-check",
        help="Only checking is supported: the skeleton is hand-written by design."),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero when a page is unreachable or an entry dangles."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Check docs/.nav.yml against the pages on disk (read-only)."""
    from memline.wiki_nav import check_nav

    if not check:
        raise typer.BadParameter(
            "the navigation skeleton is hand-written; there is nothing to generate")
    root = wiki_root or (_support.ROOT / ".agent-memory" / "wiki")
    report = check_nav(root / "content" / "docs")
    fmt = _support.chosen_format(output_format, json_flag)
    if fmt == "text":
        if not report["present"]:
            _support.console.print(f"[yellow]{report['reason']}: {report['nav_file']}[/yellow]")
        else:
            _support.console.print(f"{report['entries']} entry/entries covering "
                          f"{report['pages']} page(s)")
            for path in report["unreachable"]:
                _support.console.print(f"[yellow]unreachable: {path}[/yellow]")
            for entry in report["dangling"]:
                _support.console.print(f"[yellow]dangling entry: {entry}[/yellow]")
    _support.output(report, command="wiki-nav", fmt=fmt)
    if strict and not report["clean"]:
        raise typer.Exit(code=1)


@_support.wiki_app.command("index")
def wiki_index(
    wiki_root: Optional[Path] = typer.Argument(
        None, help="Wiki root (contains content/). Default: <workspace>/.agent-memory/wiki."),
    min_shared: int = typer.Option(3, "--min-shared",
        help="Fewest shared references that count as a relation."),
    min_share: float = typer.Option(0.15, "--min-share",
        help="Fewest shared references as a fraction of the smaller page."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Recompute the generated blocks in content/: shelf listings and relations."""
    from memline.wiki_index import refresh

    root = wiki_root or (_support.ROOT / ".agent-memory" / "wiki")
    report = refresh(root / "content", min_shared=min_shared, min_share=min_share)
    fmt = _support.chosen_format(output_format, json_flag)
    if fmt == "text":
        _support.console.print(f"{report['pages']} page(s) across {report['shelves']} shelf/shelves; "
                      f"{len(report['shelves_listed'])} listed; "
                      f"{report['relation_pairs']} relation(s); "
                      f"{len(report['written'])} file(s) rewritten")
        for path in report["pages_without_summary"]:
            _support.console.print(f"[yellow]no summary: {path}[/yellow]")
        for path in report["pages_without_topic_key"]:
            _support.console.print(f"[yellow]no topic_key: {path}[/yellow]")
        for item in report["flagged_pages"]:
            _support.console.print(f"[yellow]{item['status']}: {item['path']}[/yellow]")
    _support.output(report, command="wiki-index", fmt=fmt)


@_support.wiki_app.command("check-threads")
def wiki_check_threads(
    draft: Path = typer.Argument(..., help="Draft Markdown written by wiki-draft."),
    topics: Optional[Path] = typer.Option(
        None, "--topics", help="accepted-topics.jsonl; auto-detected for workspace drafts."),
    profiles: Optional[list[Path]] = typer.Option(
        None, "--profiles", help="Profile directories; repeatable. Auto-detected for workspace drafts."),
    show: int = typer.Option(10, "--show", help="How many dropped threads to list."),
    json_flag: bool = typer.Option(False, "--json", "--agent", help="Output JSON envelope."),
    output_format: str = typer.Option("text", "--output", "-o", help="text, json, quiet"),
) -> None:
    """Which profiled sub-topics a draft used, and which it dropped whole."""
    from memline.wiki_threads import check_draft_threads

    wiki_root = draft.resolve().parent.parent
    topics_file = topics or (wiki_root / "suggestions" / "accepted-topics.jsonl")
    dirs = list(profiles or [])
    if not dirs:
        runs = sorted((wiki_root / "suggestions" / "runs").glob("run-*"))
        if not runs:
            raise typer.BadParameter("no suggestion runs found; pass --profiles")
        dirs = [runs[-1] / "profiles", runs[-1] / "sources"]
    report = check_draft_threads(draft, topics_file, dirs)
    fmt = _support.chosen_format(output_format, json_flag)
    if fmt == "text":
        _support.console.print(
            f"{report['topic_key']}: {report['cited_evidence']}/{report['approved_evidence']} refs cited, "
            f"{report['dropped_threads']}/{report['contributing_threads']} threads dropped whole "
            f"({report['evidence_in_dropped_threads']} refs, "
            f"{report['share_of_evidence_dropped_whole']:.0%} of the topic)")
        for item in report["dropped"][:show]:
            _support.console.print(f"  [{item['members']:3} mem] {item['thread_key']}")
            _support.console.print(f"            {(item['what'] or '')[:120]}")
        if report["dropped_threads"] > show:
            _support.console.print(f"  … {report['dropped_threads'] - show} more (--show or --json for all)")
    _support.output(report, command="wiki-check-threads", fmt=fmt)
