"""Stable hashes for immutable Wiki artifacts.

Review bundles and adjudications are JSON documents whose hashes bind later
decisions to exact inputs. Keep their canonical encoding in one neutral module
so producers and validators share the contract without importing each other's
implementation details.
"""

from __future__ import annotations

import json
from typing import Any

from memline.wiki.page import sha256_text


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with stable ordering and spacing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def artifact_sha256(value: Any) -> str:
    """Hash a complete JSON-compatible artifact."""
    return sha256_text(canonical_json(value))


def content_hash(value: dict[str, Any], excluded_field: str) -> str:
    """Hash an artifact while omitting the field that stores its own hash."""
    payload = dict(value)
    payload.pop(excluded_field, None)
    return artifact_sha256(payload)
