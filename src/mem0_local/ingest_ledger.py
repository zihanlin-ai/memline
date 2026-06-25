#!/usr/bin/env python3
"""Ingest Markdown ledger files into the local mem0 store as audit chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mem0_local import cli as mem0_memory


ROOT = mem0_memory.ROOT
MEMORY_ROOT = mem0_memory.MEMORY_ROOT
MANIFEST_DIR = MEMORY_ROOT / "manifests"
LOCAL_TZ = timezone(timedelta(hours=8))


@dataclass
class Chunk:
    source_path: Path
    ledger_date: str
    start_line: int
    end_line: int
    heading_path: list[str]
    body: str

    @property
    def ledger_id(self) -> str:
        rel = self.source_path.relative_to(ROOT).as_posix()
        payload = f"{rel}:{self.start_line}:{self.end_line}:{self.body}".encode()
        return hashlib.sha256(payload).hexdigest()[:24]

    @property
    def text(self) -> str:
        rel = self.source_path.relative_to(ROOT).as_posix()
        heading = " > ".join(self.heading_path) if self.heading_path else self.ledger_date
        return (
            f"[ledger {self.ledger_date} | {heading} | {rel}:{self.start_line}-{self.end_line}]\n"
            f"{self.body}"
        )

    @property
    def metadata(self) -> dict[str, Any]:
        rel = self.source_path.relative_to(ROOT).as_posix()
        metadata = {
            "source": "agent-memory-ledger",
            "ledger_month": self.ledger_date[:7],
            "ledger_date": self.ledger_date,
            "ledger_file": rel,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "heading_path": " > ".join(self.heading_path),
            "ledger_id": self.ledger_id,
        }
        if self.start_line == self.end_line:
            metadata["line_no"] = self.start_line
        return metadata


def parse_file(path: Path, *, mode: str) -> list[Chunk]:
    lines = path.read_text(encoding="utf-8").splitlines()
    ledger_date = path.stem
    chunks: list[Chunk] = []
    headings: dict[int, str] = {}
    paragraph: list[tuple[int, str]] = []

    def heading_path() -> list[str]:
        return [headings[level] for level in sorted(headings)]

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        body = "\n".join(text for _, text in paragraph).strip()
        if body:
            chunks.append(
                Chunk(
                    source_path=path,
                    ledger_date=ledger_date,
                    start_line=paragraph[0][0],
                    end_line=paragraph[-1][0],
                    heading_path=heading_path(),
                    body=body,
                )
            )
        paragraph = []

    i = 0
    while i < len(lines):
        line_no = i + 1
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            i += 1
            continue

        if stripped.startswith("#"):
            flush_paragraph()
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            headings = {k: v for k, v in headings.items() if k < level}
            headings[level] = title
            i += 1
            continue

        if mode == "line":
            flush_paragraph()
            chunks.append(
                Chunk(
                    source_path=path,
                    ledger_date=ledger_date,
                    start_line=line_no,
                    end_line=line_no,
                    heading_path=heading_path(),
                    body=line,
                )
            )
            i += 1
            continue

        if line.startswith("- "):
            flush_paragraph()
            block: list[tuple[int, str]] = [(line_no, line)]
            i += 1
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.strip()
                if not next_stripped:
                    block.append((i + 1, next_line))
                    i += 1
                    continue
                if next_line.startswith("  ") or next_line.startswith("\t"):
                    block.append((i + 1, next_line))
                    i += 1
                    continue
                break
            body = "\n".join(text for _, text in block).strip()
            chunks.append(
                Chunk(
                    source_path=path,
                    ledger_date=ledger_date,
                    start_line=block[0][0],
                    end_line=block[-1][0],
                    heading_path=heading_path(),
                    body=body,
                )
            )
            continue

        paragraph.append((line_no, line))
        i += 1

    flush_paragraph()
    return chunks


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    entries: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            entries[item["ledger_id"]] = item
    return entries


def append_manifest(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def build_manifest_item(
    *,
    chunk: Chunk,
    metadata: dict[str, Any],
    result: dict[str, Any],
    user_id: str,
    agent_id: str,
    run_id: str,
    infer: bool,
    month: str,
    mode: str,
) -> dict[str, Any]:
    results = result.get("results", []) if isinstance(result, dict) else []
    if not isinstance(results, list):
        results = []
    memory_ids = [item.get("id") for item in results if isinstance(item, dict) and item.get("id")]
    events = [item.get("event") for item in results if isinstance(item, dict) and item.get("event")]
    result_memories = [
        item.get("memory") for item in results if isinstance(item, dict) and item.get("memory")
    ]
    line_no = chunk.start_line if chunk.start_line == chunk.end_line else None
    rel = chunk.source_path.relative_to(ROOT).as_posix()
    return {
        "schema_version": 2,
        "ledger_id": chunk.ledger_id,
        "source_entry": {
            "ledger_file": rel,
            "ledger_date": chunk.ledger_date,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "line_no": line_no,
            "heading_path": " > ".join(chunk.heading_path),
            "original_body": chunk.body,
            "add_input": chunk.text,
        },
        "timestamps": {
            "created_at": metadata.get("created_at"),
            "ledger_timestamp": metadata.get("ledger_timestamp"),
            "timestamp_source": metadata.get("timestamp_source"),
            "ingested_at": metadata.get("ingested_at"),
            "imported_at": metadata.get("imported_at"),
        },
        "request": {
            "infer": infer,
            "user_id": user_id,
            "agent_id": agent_id,
            "run_id": run_id or None,
            "mode": mode,
            "month": month,
        },
        "metadata": metadata,
        "memory_result": result,
        "events": events,
        "memory_ids": memory_ids,
        "result_memories": result_memories,
        "result_count": len(results),
    }


def parse_datetime_prefix(value: str) -> tuple[datetime, bool] | None:
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?\b", value)
    if not match:
        return None
    raw = match.group(1)
    has_time = bool(match.group(2))
    if match.group(2):
        time_part = match.group(2)
        if time_part.count(":") == 1:
            time_part = f"{time_part}:00"
        raw = f"{raw}T{time_part}"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed, has_time


def ledger_timestamp(chunk: Chunk, sequence: int) -> tuple[str, str]:
    text_time = parse_datetime_prefix(chunk.body)
    if text_time is not None:
        parsed, has_time = text_time
        if has_time:
            return parsed.replace(second=0, microsecond=0).isoformat(), "explicit_text"

    for heading in reversed(chunk.heading_path):
        heading_time = parse_datetime_prefix(heading)
        if heading_time is not None:
            parsed, has_time = heading_time
            if has_time:
                return parsed.replace(second=0, microsecond=0).isoformat(), "explicit_heading"
    parsed = datetime.fromisoformat(chunk.ledger_date).replace(tzinfo=LOCAL_TZ)
    minute_offset = max(chunk.start_line - 1, 0)
    return (parsed + timedelta(minutes=minute_offset)).replace(second=0, microsecond=0).isoformat(), "file_date_sequence"


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest .agent-memory Markdown ledgers into mem0")
    parser.add_argument("paths", nargs="+", help="Markdown files or glob patterns")
    parser.add_argument("--month", default="", help="Optional YYYY-MM metadata/import batch label")
    parser.add_argument("--user-id", default="workspace")
    parser.add_argument("--agent-id", default="ledger-importer")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--mode", choices=["line", "entry"], default="line")
    parser.add_argument(
        "--manifest-dir",
        default=str(MANIFEST_DIR),
        help="Directory for git-managed JSONL import manifests",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-infer", action="store_true", help="Store raw ledger lines without LLM extraction")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Ignore manifest and re-add chunks")
    args = parser.parse_args()

    files: list[Path] = []
    for value in args.paths:
        matches = sorted(ROOT.glob(value)) if any(ch in value for ch in "*?[]") else [Path(value)]
        for match in matches:
            path = match if match.is_absolute() else ROOT / match
            if path.is_file():
                files.append(path)

    chunks: list[Chunk] = []
    for path in sorted(set(files)):
        chunks.extend(parse_file(path, mode=args.mode))
    if args.limit:
        chunks = chunks[: args.limit]

    month = args.month or (chunks[0].ledger_date[:7] if chunks else "unknown")
    manifest_path = Path(args.manifest_dir) / f"ledger-{month}.jsonl"
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    manifest = {} if args.force else load_manifest(manifest_path)
    ordered_chunks = list(enumerate(chunks, 1))
    pending = [(sequence, chunk) for sequence, chunk in ordered_chunks if chunk.ledger_id not in manifest]

    summary = {
        "files": len(set(files)),
        "chunks_total": len(chunks),
        "chunks_pending": len(pending),
        "mode": args.mode,
        "manifest": str(manifest_path),
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.dry_run or not pending:
        for _, chunk in pending[:10]:
            preview = chunk.text.replace("\n", " ")[:220]
            print(json.dumps({"ledger_id": chunk.ledger_id, "preview": preview}, ensure_ascii=False))
        return

    client = mem0_memory.memory_client()
    imported_at = datetime.now(timezone.utc).isoformat()
    run_id = args.run_id or f"ledger-{month}"
    for index, (sequence, chunk) in enumerate(pending, 1):
        metadata = chunk.metadata
        timestamp, timestamp_source = ledger_timestamp(chunk, index)
        metadata["import_batch"] = f"ledger-{month}"
        metadata["session_id"] = run_id
        metadata["writer_agent_id"] = args.agent_id
        metadata["origin"] = "ledger_import"
        metadata["memory_schema_version"] = mem0_memory.MEMORY_SCHEMA_VERSION
        metadata["imported_at"] = imported_at
        metadata["ingested_at"] = imported_at
        metadata["created_at"] = timestamp
        metadata["ledger_timestamp"] = timestamp
        metadata["sequence"] = sequence
        metadata["timestamp_source"] = timestamp_source
        metadata["import_mode"] = args.mode
        result = client.add(
            chunk.text,
            user_id=args.user_id,
            agent_id=args.agent_id,
            run_id=run_id,
            metadata=metadata,
            infer=not args.no_infer,
        )
        item = build_manifest_item(
            chunk=chunk,
            metadata=metadata,
            result=result,
            user_id=args.user_id,
            agent_id=args.agent_id,
            run_id=run_id,
            infer=not args.no_infer,
            month=month,
            mode=args.mode,
        )
        append_manifest(manifest_path, item)
        print(json.dumps({"index": index, "of": len(pending), "ledger_id": chunk.ledger_id}, ensure_ascii=False))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
