from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path

from memline import config


class VectorStoreConfigTests(unittest.TestCase):
    def setUp(self):
        self._orig_env = os.environ.get("MEMLINE_CONFIG")
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        if self._orig_env is None:
            os.environ.pop("MEMLINE_CONFIG", None)
        else:
            os.environ["MEMLINE_CONFIG"] = self._orig_env
        importlib.reload(config)
        self._tmp.cleanup()

    def _reload_with(self, toml_text: str):
        cfg_path = Path(self._tmp.name) / "config.toml"
        cfg_path.write_text(toml_text)
        os.environ["MEMLINE_CONFIG"] = str(cfg_path)
        return importlib.reload(config)

    def test_default_is_local_path_mode(self):
        cfg = self._reload_with("")
        self.assertEqual(cfg.VECTOR_STORE_MODE, "qdrant-local-path")
        vs = cfg.vector_store_config()
        self.assertEqual(vs["provider"], "qdrant")
        self.assertEqual(vs["config"]["path"], str(cfg.QDRANT_DIR))
        self.assertNotIn("host", vs["config"])
        self.assertNotIn("port", vs["config"])

    def test_host_and_port_select_server_mode(self):
        cfg = self._reload_with('[vector_store]\nhost = "127.0.0.1"\nport = 6333\n')
        self.assertEqual(cfg.VECTOR_STORE_MODE, "qdrant-server")
        vs = cfg.vector_store_config()
        self.assertEqual(vs["config"]["host"], "127.0.0.1")
        self.assertEqual(vs["config"]["port"], 6333)
        self.assertNotIn("path", vs["config"])

    def test_host_without_port_falls_back_to_local_path(self):
        cfg = self._reload_with('[vector_store]\nhost = "127.0.0.1"\n')
        self.assertEqual(cfg.VECTOR_STORE_MODE, "qdrant-local-path")
        self.assertIn("path", cfg.vector_store_config()["config"])

    def test_server_mode_keeps_collection_and_dims(self):
        cfg = self._reload_with('[vector_store]\nhost = "127.0.0.1"\nport = 6333\n')
        vs = cfg.vector_store_config()["config"]
        self.assertEqual(vs["collection_name"], cfg.COLLECTION)
        self.assertEqual(vs["embedding_model_dims"], cfg.EMBEDDING_DIMS)
        self.assertTrue(vs["on_disk"])


if __name__ == "__main__":
    unittest.main()
