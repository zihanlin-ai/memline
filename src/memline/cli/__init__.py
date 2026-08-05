"""The command layer, one file per sub-app; shared machinery in _support.

Importing this package registers every command: _support defines the typer
apps, the command modules decorate onto them, and this file imports them in
that order. The re-exports keep every name that was reachable as
``memline.cli.X`` when this was one file reachable at the same spot.
"""

from memline.cli import _support
from memline.cli._support import (
    HYGIENE_BANNER_COMMANDS,
    ROOT,
    _apply_ttl,
    _autostart_attempted,
    _count_types,
    _dispose_with_rollback,
    _event_queue_direct,
    _interactive_tty,
    _llm_job_status,
    _load_open_pair,
    _require_disposition_authority,
    _review_artifacts,
    _sanitize_review_artifacts,
    agent_mode,
    app,
    auto_agent_output,
    autostart_enabled,
    check_raw_language,
    check_raw_length,
    chosen_format,
    cli_main,
    click,
    coerce_scalar,
    compact_json,
    confirm_destructive,
    console,
    daemon_app,
    daemon_enabled,
    daemon_spawn_safe,
    entity_app,
    err_console,
    event_app,
    execute,
    execute_queue,
    filters_from_scope,
    format_score,
    main,
    maybe_daemon_request,
    memory_client,
    normalize_timestamp,
    non_latin_letters,
    now_utc_iso,
    output,
    output_option_was_passed,
    parse_json_or_key_values,
    parse_messages_or_text,
    project_fields,
    protected_app,
    read_all_memories,
    read_content,
    render_text,
    run_invalidate,
    scope_dict,
    stale_app,
    stdin_is_piped,
    typer,
    updated_memory_metadata,
    wiki_app,
)
from memline.cli.memory import status, invalidate, revive, ttl, review, start, add, search, list_memories, get, update, delete, history, embed_test
from memline.cli.stale import stale_list, stale_confirm, stale_ttl, stale_dismiss, stale_protect, stale_unprotect, stale_protected_list, stale_merge
from memline.cli.wiki import wiki_close_run, wiki_plan, wiki_profile, wiki_bundle, wiki_suggest, wiki_draft, wiki_check_draft, wiki_prepare_review, wiki_review_draft, wiki_check_pages, wiki_nav, wiki_index, wiki_check_threads
from memline.cli.daemon import daemon_start, daemon_stop, daemon_status
from memline.cli.events import event_list, event_status, event_retry, event_ack
from memline.cli.entity import entity_list, entity_delete
