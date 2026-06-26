from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mem0_local import audit


class LiveAuditTests(unittest.TestCase):
    def test_append_live_audit_writes_monthly_jsonl_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_dir = root / "manifests"
            lock_path = root / "store" / "manifest.lock"
            result = {
                "results": [
                    {"id": "memory-1", "memory": "User prefers live audit manifests.", "event": "ADD"},
                ],
                "duration_ms": 12,
            }

            with (
                patch.object(audit, "MANIFEST_DIR", manifest_dir),
                patch.object(audit, "MANIFEST_LOCK", lock_path),
            ):
                written = audit.append_live_audit(
                    operation="add",
                    input_payload={"content": "raw input", "infer": True},
                    metadata={"source": "codex", "session_id": "session-1"},
                    result=result,
                    started_at="2026-06-26T07:00:00+00:00",
                    finished_at="2026-06-26T07:00:01+00:00",
                    duration_ms=1000,
                    scope={"user_id": "workspace", "agent_id": "codex", "run_id": "session-1"},
                )

            manifest_path = manifest_dir / "live-2026-06.jsonl"
            self.assertEqual(written["path"], str(manifest_path))
            rows = manifest_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            payload = json.loads(rows[0])
            self.assertEqual(payload["operation"], "add")
            self.assertEqual(payload["input"]["content"], "raw input")
            self.assertEqual(payload["memory_ids"], ["memory-1"])
            self.assertEqual(payload["result_memories"], ["User prefers live audit manifests."])
            self.assertEqual(payload["metadata"]["source"], "codex")
            self.assertIn("payload_sha256", payload)


if __name__ == "__main__":
    unittest.main()
